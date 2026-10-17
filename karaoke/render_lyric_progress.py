"""Render stationary lyrics with a left-to-right word sweep or an underline."""
import argparse
import json
import subprocess
import time
from bisect import bisect_right
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw

from lyric_readings import load_readings, resolve_readings
from render_subtitles import video_frame_rate
from stage_progress import progress_items, report_progress
from subtitle_style import (
    STYLES,
    TOKENS,
    CaptionOverflow,
    caption_height,
    caption_image,
    song_intro_image,
    static_video_scrim,
)
from subtitles import countdown_events, countdown_state, ktv_rows, normalized


class Caption:
    readings: tuple[tuple[int, int, str], ...]

    def __init__(self, cue, style, language, readings=None, *, height=None, style_overrides=None):
        self.cue = cue
        self.height = height or caption_height(language)
        self.style_overrides = style_overrides
        self.readings, self.reading_warnings = (readings if readings is not None else
            resolve_readings(cue['text'], language=language))
        self.layout = {}
        self.color = ImageColor.getrgb(STYLES[style].get('progress_color', '#4da3ff'))
        self.pages = []
        while True:
            try:
                self.base = caption_image(cue['text'], style=style, language=language, layout_out=self.layout,
                                          height=self.height, readings=self.readings,
                                          style_overrides=self.style_overrides)
            except CaptionOverflow:
                try:
                    self.pages = self.paginate(style, language)
                except CaptionOverflow:
                    if not self.readings:
                        raise
                    # Optional annotations cannot make an otherwise readable cue fatal.
                    self.reading_warnings += ('Reading groups exceed the available display space',)
                    self.readings = ()
                    continue
                self.base = self.pages[0].base
                self.starts = [page.cue['units'][0]['start'] for page in self.pages]
                self.countdown_geometry = self.pages[0].countdown_geometry
                return
            break
        self.starts = [u['start'] for u in cue.get('units', [])]
        self.cache = {}
        spec = TOKENS['countdown']
        self.countdown_geometry = dict(radius=spec['radius'], spacing=spec['spacing'],
            color=STYLES[style].get('progress_color', '#4da3ff'),
            y=max(spec['radius']+3, self.layout.get('content_top', min(
                c['top'] for c in self.layout['characters']))-spec['above_text']))

    def paginate(self, style, language):
        """Change only the display pages, at existing measured unit boundaries."""
        text, units = self.cue['text'], self.cue.get('units', [])
        if not units or normalized(text[:units[0]['first']]) or normalized(text[units[-1]['stop']:]) or any(
                a['stop'] > b['first'] or normalized(text[a['stop']:b['first']]) or a['start'] >= b['start']
                for a,b in zip(units, units[1:])):
            raise CaptionOverflow('Long captions require complete, ordered timing units')
        pages, first = [], 0
        while first < len(units):
            begin = units[first]['first'] if first else 0
            ends = [i for i in range(first+1, len(units)+1) if not any(
                a < (units[i]['first'] if i < len(units) else len(text)) < b for a,b,_ in self.readings)]
            low, high, fit = 0, len(ends)-1, None
            while low <= high:
                middle = (low+high)//2
                stop = ends[middle]
                end = units[stop]['first'] if stop < len(units) else len(text)
                readings = tuple((a-begin,b-begin,kana) for a,b,kana in self.readings if begin <= a and b <= end)
                try:
                    caption_image(text[begin:end], style=style, language=language,
                                  height=self.height, readings=readings, style_overrides=self.style_overrides)
                except CaptionOverflow:
                    high = middle-1
                else:
                    fit, low = stop, middle+1
            if fit is None:
                raise CaptionOverflow('One timing unit exceeds a readable page; finer acoustic units are needed')
            end = units[fit]['first'] if fit < len(units) else len(text)
            page = dict(self.cue, text=text[begin:end], units=[
                dict(unit, first=unit['first']-begin, stop=unit['stop']-begin) for unit in units[first:fit]])
            readings = tuple((a-begin,b-begin,kana) for a,b,kana in self.readings if begin <= a and b <= end)
            pages.append(Caption(page, style, language, (readings, self.reading_warnings),
                                 height=self.height, style_overrides=self.style_overrides))
            first = fit
        return pages

    def countdown(self, remaining, total):
        """Fixed dot positions above the lyric; exhausted dots become faint rings."""
        spec = self.countdown_geometry
        image = Image.new('RGBA', self.base.size)
        draw = ImageDraw.Draw(image)
        y = spec['y']
        for index in range(total):
            x = image.width/2+(index-(total-1)/2)*spec['spacing']
            radius = spec['radius']
            bounds = (x-radius, y-radius, x+radius, y+radius)
            active = index >= total-remaining
            draw.ellipse((bounds[0]-1, bounds[1]-1, bounds[2]+1, bounds[3]+1), fill=(0, 0, 0, 190))
            draw.ellipse(bounds, fill=self.color+(255,) if active else (0, 0, 0, 0),
                         outline=self.color+(255,) if active else (246, 244, 236, 160), width=2)
        return image

    def frame(self, instant, mode, *, page_at=None):
        if self.pages:
            page = max(0, bisect_right(self.starts, instant if page_at is None else page_at)-1)
            return self.pages[page].frame(instant, mode)
        index = bisect_right(self.starts, instant)-1
        if mode == 'static' or index < 0:
            return self.base
        unit = self.cue['units'][index]
        if mode == 'underline':
            next_start = self.starts[index+1] if index+1 < len(self.starts) else unit['end']+.2
            hold_end = next_start if next_start-unit['end'] <= .6 else unit['end']
            if instant >= hold_end:
                return self.base
        key = (mode, index)
        sweeping = [u for u in self.cue['units'][:index+1] if instant < u['end']] if mode == 'word' else []
        cacheable = not sweeping
        if cacheable and key in self.cache:
            return self.cache[key]
        result = self.base.copy()
        chars = self.layout['characters']
        if mode == 'word':
            area = Image.new('L', result.size)
            draw = ImageDraw.Draw(area)
            right_edges = {}
            for active in sweeping:
                selected = [c for c in chars if active['first'] <= c['index'] < active['stop']]
                fraction = (instant-active['start'])/(active['end']-active['start'])
                remaining = sum(c['right']-c['left']+4 for c in selected) * fraction
                for char in selected:
                    width = char['right']-char['left']+4
                    right_edges[char['index']] = char['left']-2+min(width, remaining)
                    remaining = max(0., remaining-width)
            for char in chars:
                right = right_edges.get(char['index'], char['right']+2)
                if char['index'] < unit['stop'] and right > char['left']-2:
                    draw.rectangle((char['left']-2, char['top']-2, right, char['bottom']+2), fill=255)
            tint = Image.new('RGBA', result.size, self.color+(0,))
            tint.putalpha(ImageChops.multiply(area, self.layout['mask']))
            result.alpha_composite(tint)
        elif mode == 'underline':
            selected = [c for c in chars if unit['first'] <= c['index'] < unit['stop'] and not c['character'].isspace()]
            draw = ImageDraw.Draw(result)
            for line in sorted({c['line'] for c in selected}):
                group = [c for c in selected if c['line'] == line]
                left, right = min(c['left'] for c in group), max(c['right'] for c in group)
                y = group[0]['baseline']+10
                draw.rounded_rectangle((left-1, y-1, right+1, y+3), radius=2, fill=(13, 17, 21, 220))
                draw.rounded_rectangle((left, y, right, y+2), radius=1, fill=self.color+(255,))
        else:
            raise ValueError(mode)
        if cacheable:
            self.cache[key] = result
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--audio', type=Path)
    parser.add_argument('--video-only', action='store_true', help='Render the picture before final audio is ready')
    parser.add_argument('--cues', type=Path, required=True)
    parser.add_argument('--readings', type=Path, help='Source-bound lyric pronunciation annotations')
    parser.add_argument('--song-info', type=Path,
                        help='JSON with title, optional artist and version for opening credits')
    parser.add_argument('--start', type=float, default=0.)
    parser.add_argument('--duration', type=float, required=True)
    parser.add_argument('--mode', choices=['word', 'underline', 'static'], default='word')
    parser.add_argument('--style', choices=STYLES, default='cinema_focus')
    parser.add_argument('--language', required=True)
    parser.add_argument('--bottom-margin', type=int, default=64)
    parser.add_argument('--next-line', action=argparse.BooleanOptionalAction, default=True,
                        help='Use equal-size fixed upper-left and lower-right lyric rows')
    parser.add_argument('--countdown', action=argparse.BooleanOptionalAction, default=True,
                        help='Three-dot countdown before vocal entries following long gaps')
    parser.add_argument('--gain', type=float, default=1.)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    cues = json.loads(args.cues.read_text())
    annotations, reading_source = load_readings(args.readings, cues, args.language) if args.readings else ([], None)
    readings = [resolve_readings(cue['text'], annotations[i]['spans'] if i < len(annotations)
                and annotations[i]['text'] == cue['text'] else (), args.language) for i, cue in enumerate(cues)]
    # A held tail can overlap the next entrance. Rendering advances by starts;
    # overlap does not invalidate the measured acoustic intervals.
    if any(c['end'] <= c['start'] for c in cues) or any(a['start'] >= b['start'] for a, b in zip(cues, cues[1:])):
        raise ValueError('Invalid display intervals')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    row_height = (TOKENS['ktv']['row_height'] +
                  (TOKENS['furigana']['extra_height'] if args.language in ('Japanese', 'Cantonese') else 0))
    width, height = 1280, row_height*2 if args.next_line else caption_height(args.language)
    overlay_top = 720-height-args.bottom_margin
    size = STYLES[args.style]['size']
    row_styles = [dict(size=size, minimum_size=size, single_line_floor=size,
                      maximum_lines=1, balanced_wrap=False, baseline_inset=TOKENS['ktv']['baseline_inset'],
                      alignment=alignment) for alignment in ('left', 'right')]
    fps = video_frame_rate(args.video)
    started = time.perf_counter()
    report_progress(detail='检查画面对比度')
    scrim_path, background = static_video_scrim(args.video, args.output.with_suffix('.contrast.png'),
        overlay_top,
        [STYLES[args.style]['color'], STYLES[args.style].get('progress_color', '#4da3ff')])
    intro = None
    intro_path = None
    if args.song_info:
        try:
            info = json.loads(args.song_info.read_text())
            first_onset = (cues[0]['units'][0]['start'] if cues and cues[0].get('units')
                           else cues[0].get('aligned_start', cues[0]['start']) if cues else float('inf'))
            end = min(TOKENS['intro']['maximum_seconds'], max(TOKENS['intro']['minimum_seconds'], first_onset))
            intro = {'information': info, 'start': 0., 'end': end, 'top': TOKENS['intro']['top'],
                     'policy': 'Credits above the always-visible lyrics; no audio or lyric timing offset',
                     'visible_in_this_export': args.start < end}
            if intro['visible_in_this_export']:
                intro_path = args.output.with_suffix('.intro.png')
                song_intro_image(info, width=width, style=args.style).save(intro_path)
        except Exception as error:
            intro_path = None
            intro = dict(status='omitted', error=type(error).__name__, reason=str(error),
                         visible_in_this_export=False)
    command = ['ffmpeg', '-v', 'error', '-nostdin', '-ss', str(args.start), '-i', str(args.video),
        '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size', f'{width}x{height}',
        '-framerate', str(fps), '-i', 'pipe:0']
    if args.audio and not args.video_only:
        command += ['-ss', str(args.start), '-i', str(args.audio)]
    audio_index = 2 if args.audio and not args.video_only else 0
    next_input = 3 if args.audio and not args.video_only else 2
    if scrim_path:
        scrim_index = next_input
        next_input += 1
        command += ['-loop', '1', '-framerate', str(fps), '-i', str(scrim_path)]
    if intro_path:
        intro_index = next_input
        command += ['-loop', '1', '-framerate', str(fps), '-i', str(intro_path)]
    filters = (
        '[0:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,'
        f'fps={fps},setpts=PTS-STARTPTS[base];[1:v]setpts=PTS-STARTPTS[text];')
    if scrim_path:
        filters += f'[base][{scrim_index}:v]overlay=0:0:format=auto[readable];'
    filters += f'[{"readable" if scrim_path else "base"}][text]overlay=0:{overlay_top}:format=auto'
    if intro_path:
        assert intro is not None
        filters += (f'[captioned];[captioned][{intro_index}:v]overlay=0:{intro["top"]}:format=auto:'
                    f'enable=\'lt(t,{intro["end"]-args.start:.9f})\'')
    command += ['-filter_complex', filters+',format=yuv420p[v]', '-map', '[v]']
    if args.video_only:
        command += ['-an']
    else:
        command += ['-map', f'{audio_index}:a:0', '-af', f'volume={args.gain:.17g},asetpts=PTS-STARTPTS',
                    '-c:a', 'aac', '-b:a', '192k']
    command += [
        '-t', str(args.duration), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19',
        '-threads', '4', '-movflags', '+faststart', str(args.output)]
    blank = Image.new('RGBA', (width, height))
    cue_starts = [c['start'] for c in cues]
    entries = countdown_events(cues, minimum_silence=TOKENS['countdown']['minimum_silence_seconds'],
        dots=TOKENS['countdown']['dots'], step_seconds=TOKENS['countdown']['step_seconds'],
        settle_seconds=TOKENS['countdown']['settle_seconds'])
    countdown = entries if args.countdown else []
    rows = ktv_rows(cues, minimum_silence=TOKENS['countdown']['minimum_silence_seconds'],
                    restart_lines={entry['line'] for entry in entries if entry['unit'] == 0})
    row_starts = [[item['start'] for item in row] for row in rows]
    slots = {item['line']: slot for slot, row in enumerate(rows) for item in row}
    previous, captions = [None, None], [None, None]
    shown, pagination, readings_report = set(), {}, {}
    log = args.output.with_suffix('.ffmpeg.log')
    with log.open('w') as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=errors)
        assert process.stdin is not None
        try:
            for frame in progress_items(range(round(args.duration*fps)), 'frames'):
                instant = args.start+frame/fps
                entry, remaining = countdown_state(countdown, instant)
                selected = [None, None]
                if args.next_line:
                    for slot, row in enumerate(rows):
                        position = bisect_right(row_starts[slot], instant)-1
                        if position >= 0 and instant < row[position]['end']:
                            selected[slot] = row[position]['line']
                    if entry is not None:
                        selected[slots[entry['line']]] = entry['line']
                else:
                    index = bisect_right(cue_starts, instant)-1
                    if index >= 0 and instant < cues[index]['end']:
                        selected[0] = index
                    if entry is not None:
                        selected[0] = entry['line']
                picture = blank.copy()
                for slot, index in enumerate(selected):
                    if index is None:
                        continue
                    if previous[slot] != index:
                        captions[slot] = Caption(cues[index], args.style, args.language, readings[index],
                            height=row_height if args.next_line else None,
                            style_overrides=row_styles[slot] if args.next_line else None)
                        caption = captions[slot]
                        if caption.pages:
                            pagination[index+1] = [dict(text=p.cue['text'], start=p.starts[0]) for p in caption.pages]
                        if args.language in ('Japanese', 'Cantonese'):
                            readings_report[index+1] = dict(spans=caption.readings, warnings=caption.reading_warnings)
                        previous[slot] = index
                    caption = captions[slot]
                    preparing = entry is not None and entry['line'] == index
                    current = caption.frame(instant, args.mode, page_at=entry['onset'] if preparing else None)
                    offset = (0, slot*row_height) if args.next_line else (0, 0)
                    picture.alpha_composite(current, offset)
                    if preparing:
                        picture.alpha_composite(caption.countdown(remaining, entry['dots']), offset)
                    shown.add(index)
                process.stdin.write(picture.tobytes())
            process.stdin.close()
            report_progress(detail='封装视频')
            if process.wait() != 0:
                raise RuntimeError(log.read_text())
        except Exception:
            process.kill()
            process.wait()
            raise
    report_progress(detail='校验视频')
    if not args.video_only:
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(args.output), '-f', 'null', '-'], check=True)
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type,width,height', '-of', 'json', str(args.output)]))
    if abs(float(probe['format']['duration'])-args.duration) > .1:
        raise ValueError('Incorrect rendered duration')
    opening = None
    if countdown and countdown[0]['start'] < 0:
        geometry = Caption(cues[0], args.style, args.language, readings[0],
            height=row_height if args.next_line else None,
            style_overrides=row_styles[0] if args.next_line else None).countdown_geometry
        opening = dict(countdown[0], **geometry, width=width, height=720)
        opening['y'] += overlay_top
    report = {'command': command, 'seconds': time.perf_counter()-started, 'start': args.start,
        'frame_rate': str(fps),
        'duration': args.duration, 'mode': args.mode, 'style': args.style, 'style_version': TOKENS['version'],
        'style_parameters': STYLES[args.style],
        'font': 'Bold Songti / Hiragino Mincho W6; source glyphs verified',
        'shown_lines': sorted(i+1 for i in shown),
        'display_pages': pagination,
        'furigana': readings_report,
        'reading_source': reading_source,
        'background_readability': background,
        'decode_check': 'pending_final_mux' if args.video_only else 'passed', 'probe': probe,
        'motion': 'Fixed text positions; hard line cuts; linear left-to-right fill within each existing timing unit',
        'next_line_enabled': args.next_line,
        'read_ahead_strategy': 'Equal-size fixed upper-left/lower-right rows; replace completed rows in place; restart upper-left after long gaps',
        'row_schedule': rows if args.next_line else [],
        'row_style_overrides': row_styles if args.next_line else [],
        'song_intro': intro,
        'opening_countdown': opening,
        'countdown': {'enabled': args.countdown, 'events': countdown,
                      'minimum_silence_seconds': TOKENS['countdown']['minimum_silence_seconds'],
                      'policy': 'Three one-second dot steps, then empty rings held for one second before the acoustic onset'},
        'fallback_lines': [i+1 for i in sorted(shown) if not cues[i].get('units')],
        'estimated_lines': [i+1 for i in sorted(shown) if any(
            u.get('timing_source') == 'linear_fallback' for u in cues[i].get('units', []))]}
    args.output.with_suffix('.render.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
