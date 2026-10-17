"""Timed subprocess output without select(), which cannot read Windows pipes."""
import os
import queue
import subprocess
import threading
import time


def output_blocks(process, command, timeout, started):
    chunks = queue.Queue(maxsize=16)
    stopped = threading.Event()

    def read():
        assert process.stdout is not None
        while not stopped.is_set():
            try:
                # Avoid BufferedReader's lock: closing it during a timed-out
                # read must not wait for a descendant holding the pipe open.
                block = os.read(process.stdout.fileno(), 65536)
            except OSError as error:
                block = error
            while not stopped.is_set():
                try:
                    chunks.put(block, timeout=.1)
                    break
                except queue.Full:
                    pass
            if not block or isinstance(block, OSError):
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        while True:
            remaining = timeout-(time.perf_counter()-started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                block = chunks.get(timeout=remaining)
            except queue.Empty:
                raise subprocess.TimeoutExpired(command, timeout) from None
            if isinstance(block, OSError):
                raise block
            yield block
            if not block:
                return
    finally:
        stopped.set()
