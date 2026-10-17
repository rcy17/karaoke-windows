"""Reobserve bounded lyric phrases after their old word timing was disproved."""
from copy import deepcopy
from typing import Any

from align_lyrics_whisper import align_candidate, valid_recovery
from lyric_progress import map_words


def realign_conflicts(cues, constraints, duration, language, align):
    from timing_validation import timing_violations, validate_timeline

    result, attempts = deepcopy(cues), []
    disputed = sorted({v['proof']['line']-1 for v in timing_violations(cues, constraints)})
    covered = set()
    for index in disputed:
        if index in covered:
            continue
        # Include a coarse following phrase whose premature start can crop the
        # target's tail. The preceding phrase supplies text context only.
        stop = index+1
        if stop < len(cues) and any(u.get('timing_source') == 'linear_fallback' for u in cues[stop]['units']):
            stop += 1
        first = max(0, index-1)
        begin = max(0., cues[first]['start']-.6)
        end = cues[stop]['start'] if stop < len(cues) else duration
        attempt: dict[str, Any] = dict(lines=[index+1, stop], context=[first, stop], source_range=[begin, end], accepted=False)
        attempts.append(attempt)
        if not 0 < end-begin <= 30:
            attempt['reason'] = 'local context exceeds 30 seconds'
            continue
        lines = [c['text'] for c in cues[first:stop]]
        try:
            raw = align_candidate(align, begin, end, '\n'.join(lines))
            attempt['result'] = raw
            if not valid_recovery(raw, lines, end-begin):
                raise ValueError('Incomplete or collapsed local word alignment')
            candidate = deepcopy(result)
            for i, segment in zip(range(first, stop), raw['segments']):
                if i < index:
                    continue
                cue = dict(cues[i], start=begin+segment['start'], end=begin+segment['end'],
                           aligned_start=begin+segment['start'], aligned_end=begin+segment['end'])
                cue['units'] = map_words(cue, segment['words'], begin, language)
                candidate[i] = cue
            # This admits a measurement for review, not for publication. Every
            # old acoustic constraint is enforced again by timing_validation.
            validate_timeline(candidate, [c['text'] for c in cues], duration, language=language)
        except ValueError as error:
            attempt['reason'] = str(error)
            continue
        result = candidate
        covered.update(range(index, stop))
        attempt['accepted'] = True
    return result, attempts


def main():
    import argparse
    import json
    from pathlib import Path

    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import resample_poly

    from align_lyrics_whisper import load_aligner

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'report', 'audio', 'lyrics', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--device', choices=['cpu', 'mps', 'cuda'], required=True)
    args = parser.parse_args()
    read = lambda p: json.loads(p.read_text())
    cues, report, lyrics = read(args.input), read(args.report), read(args.lyrics)
    info = sf.info(args.audio)
    torch.set_num_threads(4)
    torch.manual_seed(42)
    model = load_aligner('large-v3-turbo', args.device)
    from local_asr import alignment_language

    def align(begin, end, text):
        clip, rate = sf.read(args.audio, start=round(begin*info.samplerate),
                             stop=round(end*info.samplerate), dtype='float32', always_2d=True)
        divisor = np.gcd(rate, 16000)
        clip = resample_poly(clip.mean(axis=1), 16000//divisor, rate//divisor).astype('float32')
        language, _ = alignment_language(model, clip, lyrics['language'])
        aligned = model.align(clip, text, language=language,
            original_split=True, regroup=False, token_step=80, max_word_dur=None,
            word_dur_factor=None, gap_padding=None, dynamic_heads=True, verbose=None)
        return aligned.to_dict() if aligned is not None else None

    result, attempts = realign_conflicts(cues, report['constraints'], info.frames/info.samplerate,
                                        lyrics['language'], align)
    args.output.write_text(json.dumps(dict(cues=result, attempts=attempts), ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
