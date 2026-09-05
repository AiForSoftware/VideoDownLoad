'''
Function:
    Lightweight diagnostics for the desktop shell.
    Every startup stage writes an anchored line (elapsed-ms + rss + thread) into
    `<user_log_dir>/vd-desktop/startup.log`, so a hang can be located offline
    by simply looking at the last line that was written.
Author:
    CodeBuddy
'''
from __future__ import annotations

import os
import sys
import time
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()
_T0 = time.perf_counter()
_LOG_PATH: Optional[Path] = None
_MAX_BYTES = 2 * 1024 * 1024


'''logpath'''


def logpath() -> Path:
    global _LOG_PATH
    if _LOG_PATH is None:
        try:
            from platformdirs import user_log_dir
            directory = Path(user_log_dir(appname='vd-desktop', appauthor='vd'))
        except Exception:
            directory = Path.home() / '.vd-desktop'
        directory.mkdir(parents=True, exist_ok=True)
        _LOG_PATH = directory / 'startup.log'
    return _LOG_PATH


'''_rotateifneeded'''


def _rotateifneeded() -> None:
    try:
        path = logpath()
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            backup = path.with_name('startup.1.log')
            try:
                backup.unlink()
            except Exception:
                pass
            os.replace(path, backup)
    except Exception:
        pass


'''rssmb'''


def rssmb() -> float:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:
        return -1.0


'''log'''


def log(scope: str, message: str, level: str = 'info') -> None:
    elapsed_ms = round((time.perf_counter() - _T0) * 1000, 1)
    stamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    line = (f'{stamp} [+{elapsed_ms:>9}ms] [{level.upper():5}] [{scope:9}] '
            f'{message} (rss={rssmb()}MB tid={threading.get_ident()})')
    with _LOCK:
        try:
            _rotateifneeded()
            with open(logpath(), 'a', encoding='utf-8') as fp:
                fp.write(line + '\n')
                # Flush (NOT fsync) on the hot path: flush pushes the line into
                # the OS page cache, which survives process crashes and force-
                # kills — the exact forensic scenario this log exists for. The
                # old per-line os.fsync() measured ~150ms per call under
                # Defender activity, taxing every launch ~2-4s across the ~25
                # startup lines (they showed up as dead 150-220ms gaps BETWEEN
                # log lines while every step reported "done in 0.0ms"). fsync
                # is now reserved for error/crash lines where durability
                # actually matters.
                fp.flush()
                if level in ('error', 'crash'):
                    os.fsync(fp.fileno())
        except Exception:
            pass
    if os.environ.get('VD_DESKTOP_DEBUG'):
        try:
            print(line, flush=True)
        except Exception:
            pass


'''step: context manager that logs enter/exit (with duration) of a startup stage'''


@contextmanager
def step(scope: str, name: str):
    log(scope, f'>> {name}')
    started = time.perf_counter()
    try:
        yield
    except Exception as err:
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        log(scope, f'<< {name} FAILED after {elapsed}ms: {err}', 'error')
        log(scope, traceback.format_exc(), 'debug')
        raise
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    log(scope, f'<< {name} done in {elapsed}ms')


'''install_excepthooks: make any unhandled crash visible in startup.log'''


def install_excepthooks() -> None:
    def _syshook(exc_type, exc_value, exc_tb):
        log('crash', f'unhandled exception on main thread: {exc_value}', 'error')
        log('crash', ''.join(traceback.format_exception(exc_type, exc_value, exc_tb)), 'debug')

    def _threadhook(args):
        log('crash', f'unhandled exception on thread {args.thread.name}: {args.exc_value}', 'error')
        log('crash', ''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_tb)), 'debug')

    try:
        sys.excepthook = _syshook
        threading.excepthook = _threadhook
        log('diag', 'exception hooks installed')
    except Exception as err:
        log('diag', f'failed to install exception hooks: {err}', 'error')
