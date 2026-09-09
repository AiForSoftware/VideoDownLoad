'''
Function:
    Capture the download progress of vd (which is printed by `rich.progress.Progress`)
    and turn it into plain data that the webview frontend can poll.
    Also supports per-job pause/resume and per-item progress tagging.
Author:
    CodeBuddy
'''
from __future__ import annotations

import io
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


# A single shared rich.Console bound to the null sink. Creating a Console per
# Progress instance (or per NullProgress.console access) was allocating a fresh
# Console object for every download — cheap individually, but it happened once
# per parsed stream and never got released. One module-level instance is enough.
_NULL_CONSOLE = Console(file=NullFile(), width=120, quiet=True, no_color=True)


class DownloadCancelled(Exception):
    '''Raised inside the download worker when the user cancels a job.'''


class DownloadPaused(Exception):
    '''Raised inside the download worker when the user pauses a job.'''


class ProgressBus():
    '''Singleton store that mirrors the state of every rich progress task.'''

    _instance: Optional['ProgressBus'] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        # Multiple concurrent jobs may be downloading at the same time.
        # Interrupt/pause checks are keyed by job_id so one job's controls
        # do not leak into another.
        self._interrupts: Dict[str, Callable[[], bool]] = {}
        self._pauses: Dict[str, Callable[[], bool]] = {}
        # Per-thread context lets the desktop backend tag every progress task
        # created by the engine with the job/item it belongs to, so the UI can
        # show a progress bar under the correct downloading item.
        # NOTE: the engine spawns its own worker threads for segment/multi-stream
        # downloads; tasks created from those threads would get no thread-local
        # context, so we keep a global fallback (the most recently set context)
        # and use it whenever the current thread has none.
        self._context = threading.local()
        self._fallback: Dict[str, Optional[str]] = {'job_id': None, 'item_key': None}
        # Per-Progress-instance context. The engine spawns its own worker threads
        # (video/audio download, ffmpeg merge) that have no thread-local context,
        # so they previously fell back to the *global* `_fallback`, which is shared
        # process-wide and gets overwritten by whichever job set context most
        # recently — that is what made a concurrent job's merge bar show up inside
        # a different job's card. Binding the context to the Progress instance at
        # creation time (set by DesktopProgress.__init__) lets every task created
        # by that Progress — on any thread — resolve to the correct job/item.
        self._owner_ctx: Dict[str, tuple] = {}

    @classmethod
    def instance(cls) -> 'ProgressBus':
        if cls._instance is None:
            with cls._instance_lock:
                cls._instance = cls() or cls._instance
        return cls._instance

    def set_context(self, job_id: str, item_key: str) -> None:
        '''Set the (job_id, item_key) context for the current thread.'''
        self._context.job_id = job_id
        self._context.item_key = item_key
        self._fallback['job_id'] = job_id
        self._fallback['item_key'] = item_key

    def clear_context(self) -> None:
        '''Clear the per-thread context.'''
        self._context.job_id = None
        self._context.item_key = None

    def _current_context(self):
        '''(job_id, item_key) for the current thread, falling back to the most
        recently set context for engine-spawned threads that have none.'''
        job_id = getattr(self._context, 'job_id', None)
        item_key = getattr(self._context, 'item_key', None)
        if job_id is not None or item_key is not None:
            return job_id, item_key
        return self._fallback['job_id'], self._fallback['item_key']

    def current_context(self):
        '''Public accessor used by DesktopProgress at construction time to bind
        the freshly created Progress instance to whatever job/item is currently
        running on the spawning thread.'''
        return self._current_context()

    def set_owner_context(self, owner: str, job_id, item_key) -> None:
        '''Bind (job_id, item_key) to a Progress instance (`owner`). Every progress
        task created by that instance resolves to this context regardless of which
        thread (engine-spawned or not) creates it.'''
        if job_id is None and item_key is None:
            self._owner_ctx.pop(owner, None)
        else:
            self._owner_ctx[owner] = (job_id, item_key)

    def add_interrupt(self, key: str, func: Optional[Callable[[], bool]]) -> None:
        with self._lock:
            if func is None:
                self._interrupts.pop(key, None)
            else:
                self._interrupts[key] = func

    def remove_interrupt(self, key: str) -> None:
        with self._lock:
            self._interrupts.pop(key, None)

    def add_pause(self, key: str, func: Optional[Callable[[], bool]]) -> None:
        with self._lock:
            if func is None:
                self._pauses.pop(key, None)
            else:
                self._pauses[key] = func

    def remove_pause(self, key: str) -> None:
        with self._lock:
            self._pauses.pop(key, None)

    def checkinterrupt(self) -> None:
        # Pause/cancel are per-job: only the *current* job (resolved from the
        # calling thread's context) may raise. Iterating every registered check
        # would leak one job's pause/cancel into all concurrent downloads.
        job_id, _ = self._current_context()
        if not job_id:
            return
        with self._lock:
            check = self._interrupts.get(job_id)
        if check is not None and check():
            raise DownloadCancelled('the download job has been cancelled by the user')

    def checkpause(self) -> None:
        job_id, _ = self._current_context()
        if not job_id:
            return
        with self._lock:
            check = self._pauses.get(job_id)
        if check is not None and check():
            raise DownloadPaused('the download job has been paused by the user')

    def check_controls(self) -> None:
        '''Check BOTH pause and cancel under a single lock take.

        `update()` runs on the byte-level progress hot path and used to call
        `checkpause()` then `checkinterrupt()` — each acquiring the RLock
        separately. Doing it once avoids the redundant lock round-trip on every
        progress tick (which can fire hundreds of times per second per stream).'''
        job_id, _ = self._current_context()
        if not job_id:
            return
        with self._lock:
            pause_check = self._pauses.get(job_id)
            interrupt_check = self._interrupts.get(job_id)
        if pause_check is not None and pause_check():
            raise DownloadPaused('the download job has been paused by the user')
        if interrupt_check is not None and interrupt_check():
            raise DownloadCancelled('the download job has been cancelled by the user')

    def onadd(self, owner: str, task_id: Any, description: str, total: Optional[float], fields: Dict[str, Any]) -> None:
        key = f'{owner}:{task_id}'
        ctx_job, ctx_item = self._current_context()
        # Prefer the Progress-instance binding: engine-spawned threads (video/audio
        # download, ffmpeg merge) have no thread-local context and the global
        # `_fallback` is shared across all concurrent jobs, so it can point at the
        # wrong job. The owner binding is set once at Progress creation and is the
        # authoritative context for every task this instance creates.
        ob = self._owner_ctx.get(owner)
        if (ctx_job is None and ctx_item is None) and ob:
            ctx_job, ctx_item = ob
        with self._lock:
            self._tasks[key] = {
                'key': key, 'description': str(description), 'completed': 0.0, 'total': total,
                'kind': fields.get('kind', 'download'), 'speed': None, 'finished': False,
                'started_at': time.time(), 'updated_at': time.time(),
                'job_id': ctx_job, 'item_key': ctx_item,
            }

    def onupdate(self, owner: str, task_id: Any, task: Any = None) -> None:
        key = f'{owner}:{task_id}'
        with self._lock:
            item = self._tasks.get(key)
            if item is None:
                ctx_job, ctx_item = self._current_context()
                ob = self._owner_ctx.get(owner)
                if (ctx_job is None and ctx_item is None) and ob:
                    ctx_job, ctx_item = ob
                item = {
                    'key': key, 'description': '', 'completed': 0.0, 'total': None,
                    'kind': 'download', 'speed': None, 'finished': False,
                    'started_at': time.time(), 'job_id': ctx_job, 'item_key': ctx_item,
                }
                self._tasks[key] = item
            else:
                ctx_job = item.get('job_id')
                ctx_item = item.get('item_key')
            # Engine-spawned worker threads have no thread-local context of their
            # own, so they would otherwise fall back to whichever job set context
            # most recently (possibly a *different* job). Bind the calling thread
            # to this task's job the first time we see it, so the pause/cancel
            # checks performed inside the worker target the correct job — this is
            # what stops pausing one job from pausing a concurrent download.
            if getattr(self._context, 'job_id', None) is None and ctx_job:
                self.set_context(ctx_job, ctx_item or '')
            if task is not None:
                item['description'] = str(getattr(task, 'description', item['description']) or '')
                item['completed'] = float(getattr(task, 'completed', 0.0) or 0.0)
                item['total'] = getattr(task, 'total', item['total'])
                item['finished'] = bool(getattr(task, 'finished', False))
                # 'audio' streams are byte-based just like 'download' ones and must
                # report a speed too; 'm3u8download' counts segments (not bytes) so
                # its "speed" would be meaningless in the byte-based totals.
                try:
                    item['speed'] = task.speed if item['kind'] in ('download', 'audio') else None
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

    def finish_tasks_for(self, job_id: str, item_key: Optional[str] = None) -> None:
        '''Mark all progress tasks belonging to (job_id, item_key) as finished.
        Called when an item is paused/cancelled/resumed so the next run does
        not stack its bytes on top of stale progress tasks in the UI.'''
        with self._lock:
            for item in self._tasks.values():
                if item.get('job_id') != job_id:
                    continue
                if item_key is not None and item.get('item_key') != item_key:
                    continue
                item['finished'] = True
                item['updated_at'] = time.time()

    # Finished tasks only matter for a beat (the UI shows a bar under the active
    # item, then the card flips to a terminal state). Keep them for this window so
    # a just-finished item can still report its final numbers, then reclaim them —
    # otherwise a long session (many downloads) let `_tasks` grow without bound,
    # dragging every 700-1500ms `snapshot()` into a full scan of all history.
    _RECLAIM_AFTER_S = 300

    def snapshot(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            expired = [
                k for k, v in self._tasks.items()
                if v.get('finished') and (now - v.get('updated_at', 0)) > self._RECLAIM_AFTER_S
            ]
            for k in expired:
                self._tasks.pop(k, None)
            # Drop owner contexts whose Progress instance no longer has any task.
            if self._owner_ctx:
                live_owners = {k.split(':', 1)[0] for k in self._tasks}
                for owner in [o for o in self._owner_ctx if o not in live_owners]:
                    self._owner_ctx.pop(owner, None)
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
                'job_id': item.get('job_id'), 'item_key': item.get('item_key'),
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
        kwargs.setdefault('console', _NULL_CONSOLE)
        super(DesktopProgress, self).__init__(*columns, **kwargs)
        self._owner = f'p{id(self):x}'
        # Bind this Progress instance to whatever job/item is running on the
        # spawning thread right now. Every task created by this instance — on any
        # thread, including engine-spawned merge/audio workers — then resolves to
        # the correct job/item, so a concurrent job's merge bar can no longer land
        # inside a different job's card.
        try:
            bus = ProgressBus.instance()
            bus.set_owner_context(self._owner, *bus.current_context())
        except Exception:
            pass

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
        bus = ProgressBus.instance()
        bus.onupdate(self._owner, task_id, self._tasks.get(task_id))
        bus.check_controls()
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
        return _NULL_CONSOLE


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
            self._console = _NULL_CONSOLE
            self._progress = NullProgress()

        def ensurestarted(self) -> None:
            self._started = True

        def log(self, *args, **kwargs) -> None:
            return None

    vd_utils_progress.GlobalProgressManager = DesktopGlobalProgressManager
