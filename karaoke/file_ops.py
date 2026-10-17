"""Atomic publication with bounded retries for Windows sharing violations."""
import os
import time

WINDOWS = os.name == 'nt'


def replace_file(source, target):
    # Readers/antivirus may hold a Windows handle without FILE_SHARE_DELETE.
    # Never replace an atomic rename with unlink/copy: readers must see a whole file.
    for attempt in range(21):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if not WINDOWS or error.winerror not in (5, 32, 33) or attempt == 20:
                raise
            time.sleep(.05)
