"""Process-owned exclusive locks, including readable lock records on Windows."""
import errno
import os
import time


def lock_file(file, *, blocking=True):
    if os.name != 'nt':
        import fcntl
        fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    import msvcrt
    position = file.tell()
    # Windows byte locks are mandatory. Lock beyond our small JSON records so
    # other workers can still read the owner/priority while the lock is held.
    try:
        file.seek(0x7fffffff)
        while True:
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if not blocking:
                    raise BlockingIOError(errno.EAGAIN, 'File is locked') from error
                time.sleep(.05)
    finally:
        file.seek(position)


def unlock_file(file):
    if os.name != 'nt':
        import fcntl
        fcntl.flock(file, fcntl.LOCK_UN)
        return
    import msvcrt
    position = file.tell()
    try:
        file.seek(0x7fffffff)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        file.seek(position)
