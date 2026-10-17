"""Keep worker descendants inside the worker lifetime on Windows and POSIX."""
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def worker_command(command):
    if os.name == 'nt':
        return [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), *map(str, command)]
    return command


def signal_tree(pid, *, force=False):
    try:
        if os.name == 'nt':
            # The wrapper owns a kill-on-close Job Object, so terminating it also
            # kills descendants even when the actual pipeline has already exited.
            stop_windows_job(pid)
        else:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


async def terminate_tree(process):
    if os.name != 'nt' or process.returncode is None:
        await asyncio.to_thread(signal_tree, process.pid)
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        pass
    finally:
        if os.name != 'nt' or process.returncode is None:
            signal_tree(process.pid, force=True)
    await process.wait()


def capture(command, *, input, cwd, env, timeout):
    """Bound a CLI request, including children launched by npm's Node entry."""
    with subprocess.Popen(worker_command(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding='utf-8', cwd=cwd, env=env,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException:
            signal_tree(process.pid, force=True)
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def stop_windows_job(pid):
    import ctypes
    from ctypes import wintypes

    class Accounting(ctypes.Structure):
        _fields_ = [('times', ctypes.c_int64 * 4), ('page_faults', wintypes.DWORD),
                    ('total', wintypes.DWORD), ('active', wintypes.DWORD), ('terminated', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.DWORD, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenJobObjectW(0x0004 | 0x0008, False, f'Local\\karaoke-worker-{pid}')
    if not handle:
        if ctypes.get_last_error() != 2:
            raise ctypes.WinError(ctypes.get_last_error())
        # Cancelled before wrapper initialization, or already fully exited.
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            if error.winerror != 87:
                raise
        return
    try:
        if not kernel.TerminateJobObject(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic()+5
        while True:
            accounting = Accounting()
            if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not accounting.active:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('Windows worker descendants did not terminate')
            time.sleep(.01)
    finally:
        kernel.CloseHandle(handle)


def windows_job():
    """Assign this wrapper before it spawns anything; closing the handle kills all."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                    ('max_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]

    class Limits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io', ctypes.c_uint64 * 6),
                    ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, f'Local\\karaoke-worker-{os.getpid()}')
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = Limits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    # Keep the handle open until process exit, including when the child fails.
    return handle


if __name__ == '__main__':
    handle = windows_job() if os.name == 'nt' else None
    raise SystemExit(subprocess.call(sys.argv[1:], env={**os.environ, 'PYTHONUTF8': '1'}))
