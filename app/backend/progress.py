'''
Function:
    Capture the download progress of vd (which is printed by `rich.progress.Progress`)
    and turn it into plain data that the webview frontend can poll.
Author:
    CodeBuddy
'''
from __future__ import annotations

import io
import sys
import time
import threading
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console
from rich.progress import Progress as RichProgress


class NullFile():
    '''A sink file object so that rich never writes escape sequences into a GUI process.'''
    def write(self, *args, **kwargs):
        return 0

    def writelines(self, *args, **kwargs):
        return None

    def flush(self):
        return None

    def isatty(self) -> bool:
        return False

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation('fileno')


class DownloadCancelled(Exception):
    '''Raised inside the download worker when the user cancels a job.'''


class ProgressBus():
    '''Singleton store that mirrors the state of every rich progress task.'''

    _instance: Optional['ProgressBus'] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._interrupt: Optional[Callable[[], bool]] = None

    @classmethod
    def instance(cls) -> 'ProgressBus':
        if cls._instance is None:
            with cls._instance_lock:
                cls._instance = cls._instance or cls()
        return cls._instance

    def set_interrupt(self, func: Optional[Callable[[], bool]]) -> None:
        with self._lock:
            self._interrupt = func

    def checkinterrupt(self) -> None:
        with self._lock:
            interrupt = self._interrupt
        if interrupt is not None and interrupt():
            raise DownloadCancelled('the download job has been cancelled by the user')

    def onadd(self, owner: str, task_id: Any, description: str, total: Optional[float], fields: Dict[str, Any]) -> None:
        key = f'{owner}:{task_id}'
        with self._lock:
            self._tasks[key] = {
                'key': key, 'description': str(description), 'completed': 0.0, 'total': total,
                'kind': fields.get('kind', 'download'), 'speed': None, 'finished': False,
                'started_at': time.time(), 'updated_at': time.time(),
            }

    def onupdate(self, owner: str, task_id: Any, task: Any = None) -> None:
        key = f'{owner}:{task_id}'
        with self._lock:
            item = self._tasks.get(key)
            if item is None:
                item = {'key': key, 'description': '', 'completed': 0.0, 'total': None, 'kind': 'download', 'speed': None, 'finished': False, 'started_at': time.time()}
                self._tasks[key] = item
            if task is not None:
                item['description'] = str(getattr(task, 'description', item['description']) or '')
                item['completed'] = float(getattr(task, 'completed', 0.0) or 0.0)
                item['total'] = getattr(task, 'total', item['total'])
                item['finished'] = bool(getattr(task, 'finished', False))
                try:
                    item['speed'] = task.speed if item['kind'] == 'download' else None
                except Exception:
                    item['speed'] = None
                fields = getattr(task, 'fields', None) or {}
                if 'kind' in fields:
                    item['kind'] = fields['kind']
            item['updated_at'] = time.time()

    def onremove(self, owner: str, task_id: Any) -> None:
        key = f'{owner}:{task_id}'
        with self._lock:
            if (item := self._tasks.get(key)) is not None:
                item['finished'] = True
                item['updated_at'] = time.time()

    def reset(self) -> None:
        with self._lock:
            self._tasks.clear()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._tasks.values())
        result: List[Dict[str, Any]] = []
        for item in items:
            total, completed = item.get('total'), item.get('completed') or 0.0
            percent = None
            if total:
                try:
                    percent = max(0.0, min(100.0, completed / float(total) * 100.0))
                except Exception:
                    percent = None
            speed = item.get('speed')
            eta = None
            if speed and total and not item.get('finished'):
                try:
                    if float(speed) > 0:
                        eta = round(float(total - completed) / float(speed), 1)
                except Exception:
                    eta = None
            result.append({
                'key': item['key'], 'description': item['description'], 'kind': item['kind'],
                'completed': completed, 'total': total, 'percent': percent, 'speed': speed, 'eta': eta,
                'finished': bool(item['finished']), 'elapsed': round(time.time() - item['started_at'], 1),
            })
        result.sort(key=lambda x: (x['kind'] != 'overall', x['description']))
        return result


'''Build a `rich.progress.Progress` subclass that never spins up a `Live`
renderer — calling `start()` / `stop()` is a no-op, so no console escape
sequences get written into the GUI process (which would otherwise manifest
as a brief black console window on Windows).

Kept at module scope (NOT nested inside `install_progress_hook`) so that
PyInstaller's byte-code compiler can resolve the symbol reliably when the
frozen bundle boots.'''


class DesktopProgress(RichProgress):
    _vd_desktop_hook = True

    def __init__(self, *columns, **kwargs):
        kwargs.setdefault('console', Console(file=NullFile(), width=120, quiet=True, no_color=True))
        super(DesktopProgress, self).__init__(*columns, **kwargs)
        self._owner = f'p{id(self):x}'

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def add_task(self, description: str, start: bool = True, total: float = 100, completed: int = 0, visible: bool = True, **fields):
        task_id = super(DesktopProgress, self).add_task(description, start=start, total=total, completed=completed, visible=visible, **fields)
        ProgressBus.instance().onadd(self._owner, task_id, description, total, fields)
        return task_id

    def update(self, task_id, **kwargs):
        result = super(DesktopProgress, self).update(task_id, **kwargs)
        ProgressBus.instance().onupdate(self._owner, task_id, self._tasks.get(task_id))
        ProgressBus.instance().checkinterrupt()
        return result

    def remove_task(self, task_id):
        ProgressBus.instance().onremove(self._owner, task_id)
        return super(DesktopProgress, self).remove_task(task_id)


class NullProgress:
    '''Drop-in stand-in for `rich.progress.Progress` used by the patched
    `GlobalProgressManager`. Same surface as `rich.progress.Progress` but
    no Live renderer.
    '''
    def start(self) -> None: return None
    def stop(self) -> None: return None
    def add_task(self, *args, **kwargs): return 0
    def update(self, task_id, **kwargs): return None
    def remove_task(self, task_id): return None
    @property
    def console(self):
        return Console(file=NullFile(), width=120, quiet=True, no_color=True)


'''Marker printed at module import time so we can confirm (in the frozen
bundle's startup log) which file PyInstaller actually bundled.'''


sys.stderr.write(f'[vd_desktop.progress] loaded from {__file__}\n')
sys.stderr.flush()


def install_progress_hook() -> None:
    '''Replace `Progress` inside `vd.modules.sources.base` *and*
    `GlobalProgressManager` inside `vd.modules.utils.progress` with
    instrumented subclasses that do not start Rich `Live` renderers.
    '''
    _patch_desktop_progress()
    _patch_global_progress_manager()


def _patch_desktop_progress() -> None:
    try:
        from vd.modules.sources import base as vd_base
    except Exception:
        return
    if getattr(vd_base.Progress, '_vd_desktop_hook', False):
        return
    vd_base.Progress = DesktopProgress


def _patch_global_progress_manager() -> None:
    try:
        from vd.modules.utils import progress as vd_utils_progress
    except Exception:
        return
    MgrCls = getattr(vd_utils_progress, 'GlobalProgressManager', None)
    if MgrCls is None or getattr(MgrCls, '_vd_desktop_hook', False):
        return

    class DesktopGlobalProgressManager(MgrCls):
        _vd_desktop_hook = True

        def __init__(self) -> None:
            # skip MgrCls.__init__ entirely so it does NOT build a real
            # rich.progress.Progress (whose start() would launch a Live).
            self._started = False
            self._active_tasks = 0
            self._lock = threading.RLock()
            self._console = Console(file=NullFile(), width=120, quiet=True, no_color=True)
            self._progress = NullProgress()

        def ensurestarted(self) -> None:
            self._started = True

        def log(self, *args, **kwargs) -> None:
            return None

    vd_utils_progress.GlobalProgressManager = DesktopGlobalProgressManager