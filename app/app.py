'''
Function:
    Entry point of the vd desktop shell (pywebview + Edge WebView2)
Author:
    CodeBuddy
'''
from __future__ import annotations

import os
import sys
import time
import argparse
import subprocess
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any, Dict

DESKTOP_ROOT = Path(__file__).resolve().parent
if str(DESKTOP_ROOT) not in sys.path:
    sys.path.insert(0, str(DESKTOP_ROOT))

from backend import diag  # noqa: E402  (diagnostics must be importable as early as possible)
import psutil  # noqa: E402  (used by the single-instance guard to reap a stuck previous instance)

APP_NAME = '全能下载器'
APP_VERSION = '1.1.0'
SELFTEST_URL = 'https://www.bilibili.com/video/BV1GJ411x7h7'


'''resource path (works both in dev mode and inside a PyInstaller bundle)'''


def resourcepath(relative: str) -> Path:
    base = Path(getattr(sys, '_MEIPASS', DESKTOP_ROOT))
    return base / relative


'''stdio: a frozen GUI process has no console, redirect to a log file to avoid crashes'''


def setupstdio() -> None:
    if not getattr(sys, 'frozen', False):
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from platformdirs import user_log_dir
        log_dir = Path(user_log_dir(appname='vd-desktop', appauthor='vd'))
    except Exception:
        log_dir = Path.home() / '.vd-desktop'
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        stream = open(log_dir / 'desktop.log', 'a', encoding='utf-8', errors='ignore')
        sys.stdout = sys.stdout if sys.stdout is not None else stream
        sys.stderr = sys.stderr if sys.stderr is not None else stream
    except Exception:
        sys.stdout = sys.stdout if sys.stdout is not None else open(os.devnull, 'w', encoding='utf-8')
        sys.stderr = sys.stderr if sys.stderr is not None else open(os.devnull, 'w', encoding='utf-8')


'''selftest: run a real parse+download without opening a window, and dump the report to a file.
   It is used to validate a packaged (windowed, console-less) build end to end.'''


def runselftest(url: str) -> int:
    import json
    import time
    from pathlib import Path
    from backend.core import VideoDlService

    work_dir = Path.home() / 'vd_selftest_output'
    work_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path.home() / 'vd_selftest_result.json'
    report: Dict[str, Any] = {'url': url, 'work_dir': str(work_dir), 'frozen': bool(getattr(sys, 'frozen', False))}
    try:
        service = VideoDlService(version=APP_VERSION)
        service.config.work_dir = str(work_dir)
        parsed = service.parse(url)
        items = parsed.get('items') or []
        report['parse_ok'] = bool(parsed.get('ok'))
        report['parse_error'] = parsed.get('error') or ''
        report['parsed_items'] = [{'title': i['title'], 'source': i['source'], 'ext': i['ext'], 'valid': i['valid']} for i in items]
        keys = [i['key'] for i in items if i['valid']]
        if keys:
            job = service.enqueue(keys)
            job_id = job.get('job_id')
            report['job_id'] = job_id
            deadline = time.time() + 240
            while time.time() < deadline:
                current = service._getjob(job_id)
                if current is None or current['status'] not in {'queued', 'downloading', 'cancelling'}:
                    break
                time.sleep(0.5)
            current = service._getjob(job_id) or {}
            report['job_status'] = current.get('status')
            report['job_done'] = current.get('done_count')
            report['job_total'] = current.get('total_count')
            report['files'] = [
                {'title': it['title'], 'status': it['status'], 'save_path': it['save_path'], 'exists': Path(it['save_path']).exists()}
                for it in current.get('items', [])
            ]
        report['logs'] = service.logs(0)[-25:]
        success = report.get('parse_ok') and report.get('job_status') == 'done' and report.get('job_done') == report.get('job_total')
        report['success'] = bool(success)
    except Exception as err:
        report['success'] = False
        report['fatal'] = f'{err}\n{traceback.format_exc()}'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['success'] else 1


'''single instance guard: two instances fight over config.json / WebView2 data / logs and may hang'''


_singleton_pidfile = None


def _releasesingleton() -> None:
    '''Remove our pid lock on a clean exit (only if it still belongs to us).'''
    try:
        if _singleton_pidfile is not None and _singleton_pidfile.exists():
            if _singleton_pidfile.read_text(encoding='utf-8').strip() == str(os.getpid()):
                _singleton_pidfile.unlink()
    except Exception:
        pass


def _focus_existing_window() -> bool:
    '''Bring the already-running UI window to the foreground. Returns True when a
    window was found. Used instead of killing a healthy instance when the user
    double-clicks again while the first launch is still coming up.'''
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, f'{APP_NAME} v{APP_VERSION}')
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def _window_is_hung(hwnd) -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsHungAppWindow(hwnd))
    except Exception:
        return False


def _findwindow() -> int:
    '''HWND of this app's main window (0 when not created or already closed).'''
    try:
        import ctypes
        return ctypes.windll.user32.FindWindowW(None, f'{APP_NAME} v{APP_VERSION}') or 0
    except Exception:
        return 0


def _notify_already_running(focused: bool) -> None:
    '''Native reminder popup: the app is already running — do not open it twice.'''
    try:
        import ctypes
        msg = ('全能下载器已在运行中，请勿重复打开。\n'
               + ('已为你切换到已打开的窗口。' if focused else '请查看任务栏中已打开的应用窗口。'))
        # MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
        ctypes.windll.user32.MessageBoxW(0, msg, APP_NAME, 0x40 | 0x10000 | 0x40000)
    except Exception:
        pass


def acquiresingleinstance() -> bool:
    '''Ensure a single instance WITHOUT killing healthy launches.

    Previously a second double-click TERMINATED the already-running instance —
    and because a cold first launch can take 10-30s (Defender scanning the
    freshly built files + WebView2 init), users kept clicking impatiently and
    every click killed the startup-in-progress, restarting the whole slow cycle.
    That is why "the app only starts after several clicks".

    New behaviour:
      * no previous instance  -> acquire the lock and start normally;
      * previous instance alive and its window responds -> focus it, show a
        "already running, do not open twice" reminder and exit quietly;
      * previous instance alive but its window does NOT exist yet -> it is a
        cold start in progress, never kill it. Wait up to 20s for the window to
        appear, then either focus it (and remind) or exit quietly — its own
        supervisor is responsible for recovering a genuinely hung init;
      * previous instance alive with a HUNG window (IsHungAppWindow) -> kill its
        whole process tree and start fresh (the old recovery, now precise).'''
    global _singleton_pidfile
    try:
        from platformdirs import user_log_dir
        d = Path(user_log_dir(appname='vd-desktop', appauthor='vd'))
    except Exception:
        d = Path.home() / '.vd-desktop'
    try:
        d.mkdir(parents=True, exist_ok=True)
        _singleton_pidfile = d / 'singleton.pid'
        if _singleton_pidfile.exists():
            try:
                oldpid = int(_singleton_pidfile.read_text(encoding='utf-8').strip() or '0')
            except Exception:
                oldpid = 0
            if oldpid and psutil.pid_exists(oldpid):
                hwnd = _findwindow()
                if hwnd and not _window_is_hung(hwnd):
                    diag.log('app', f'another instance (pid={oldpid}) is running and responsive; '
                                    f'focusing its window and exiting', 'info')
                    _focus_existing_window()
                    _notify_already_running(focused=True)
                    return False
                if hwnd and _window_is_hung(hwnd):
                    diag.log('app', f'another instance (pid={oldpid}) is running but hung; '
                                    f'terminating it to recover', 'warning')
                    try:
                        proc = psutil.Process(oldpid)
                        for child in proc.children(recursive=True):
                            try: child.kill()
                            except Exception: pass
                        proc.kill()
                    except Exception as err:
                        diag.log('app', f'failed to terminate previous instance pid={oldpid}: {err}', 'warning')
                else:
                    # Window not created yet: a cold start is in progress (WebView2 init
                    # on a cold boot can take 15-30s). Killing it here is exactly what
                    # made repeated double-clicks "unstart" the app. Wait briefly for
                    # the window to come up, then remind the user instead of racing it.
                    diag.log('app', f'another instance (pid={oldpid}) is starting (no window yet); '
                                    f'waiting for it to come up instead of killing it', 'info')
                    _deadline = time.monotonic() + 20
                    _up = False
                    while time.monotonic() < _deadline:
                        hwnd = _findwindow()
                        if hwnd and not _window_is_hung(hwnd):
                            _up = True
                            break
                        if not psutil.pid_exists(oldpid):
                            break
                        time.sleep(0.5)
                    if _up and psutil.pid_exists(oldpid):
                        diag.log('app', f'previous instance (pid={oldpid}) window appeared; focusing it', 'info')
                        _focus_existing_window()
                        _notify_already_running(focused=True)
                        return False
                    if psutil.pid_exists(oldpid):
                        # Still no window after 20s. Its own supervisor will kill and
                        # retry a hung WebView2 init — we must not fight it. Remind and
                        # exit so we never end up with two competing instances.
                        diag.log('app', f'previous instance (pid={oldpid}) still has no window after 20s; '
                                        f'exiting to avoid a duplicate instance', 'warning')
                        _notify_already_running(focused=False)
                        return False
        _singleton_pidfile.write_text(str(os.getpid()), encoding='utf-8')
        import atexit
        atexit.register(_releasesingleton)
        return True
    except Exception as err:
        diag.log('app', f'single instance guard unavailable: {err}', 'warning')
        return True


'''prepend a bundled tools directory to PATH so N_m3u8DL-RE, aria2c, etc.
are discoverable without requiring the user to install them globally.
Works both in development (project-root/bin) and in a PyInstaller bundle.'''


def setupbundledtools() -> None:
    candidates = [
        resourcepath('bin'),
        DESKTOP_ROOT.parent / 'bin',
        Path(getattr(sys, 'executable', '')).parent / 'bin',
        Path(getattr(sys, 'executable', '')).parent,
    ]
    for cand in candidates:
        exe = cand / 'N_m3u8DL-RE.exe'
        if cand.exists() and cand.is_dir() and str(cand) not in os.environ.get('PATH', '').split(os.pathsep):
            os.environ['PATH'] = str(cand) + os.pathsep + os.environ.get('PATH', '')
            diag.log('app', f'added bundled tools dir to PATH: {cand} (N_m3u8DL-RE found={exe.exists()})')
            break


'''isolate the WebView2 user-data folder so instances / other pywebview apps never collide'''


def setupwebviewdatafolder() -> None:
    try:
        from platformdirs import user_data_dir
        folder = Path(user_data_dir(appname='vd-desktop', appauthor='vd')) / 'webview2'
    except Exception:
        folder = Path.home() / '.vd-desktop' / 'webview2'
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.environ['WEBVIEW2_USER_DATA_FOLDER'] = str(folder)
        diag.log('app', f'webview2 user data folder: {folder}')
    except Exception as err:
        diag.log('app', f'failed to prepare webview2 data folder: {err}', 'warning')
    # Trim WebView2's startup work: the loader honors this env var with higher
    # priority than pywebview's programmatic AdditionalBrowserArguments, so the
    # ElasticOverscroll feature-disable pywebview sets is folded in here.
    # Measured pain points on cold start: component-update fetches, background
    # networking and SmartScreen checks each add seconds (or hang on a flaky
    # network) BEFORE the first navigation even begins.
    os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = (
        '--disable-component-update --disable-background-networking '
        '--disable-domain-reliability --disable-sync --no-first-run --noerrdialogs '
        '--disable-features=ElasticOverscroll,msSmartScreenProtection'
    )
    diag.log('app', 'webview2 startup args: component-update/background-networking/SmartScreen disabled')


'''kill orphaned webview2 processes that belong to THIS app's WebView2 data folder.

A previous force-killed instance leaves msedgewebview2.exe processes running; they
deadlock webview.start() on the next launch (window appears but the app hangs).
We match by the data-folder path in the command line so we never touch other apps'
WebView2 processes (e.g. VS Code).'''


def _kill_procs_by_cmdline(name: str, substr: str, timeout: float = 6.0) -> int:
    '''Kill processes whose name matches `name` AND whose command line contains `substr`.

    Uses psutil instead of a global `taskkill /f /im msedgewebview2.exe`. Two reasons:
      * a global taskkill hangs for the full 20s timeout when a pile of orphaned webview
        processes has built up, and that 20s tax on every launch is what made the app
        take minutes to open (it blew past the 30s UI-load timeout and forced retries);
      * matching by command line lets us touch ONLY this app's WebView2 instances (they
        all carry our WEBVIEW2_USER_DATA_FOLDER path), never other apps' webviews.'''
    import psutil
    killed = 0
    targets = []
    try:
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if p.info['name'] and p.info['name'].lower() == name.lower():
                    cmd = ' '.join(p.info['cmdline'] or '')
                    if substr and substr.lower() in cmd.lower():
                        targets.append(p)
            except Exception:
                continue
    except Exception as err:
        diag.log('app', f'_kill_procs_by_cmdline scan failed: {err}', 'debug')
        return 0
    for p in targets:
        try:
            for child in p.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            p.kill()
            killed += 1
        except Exception:
            pass
    return killed


def _releasestaleprofilelock(user_data_folder: str) -> None:
    '''Remove the Chromium profile lockfile left behind by a force-killed instance.
    When no live msedgewebview2 process holds it the file is garbage; leaving it in
    place can stall the next webview.start() for the whole UI-load timeout (measured:
    60s of dead silence right after a force-kill, while the very next attempt loaded
    in 2s). Only ever called when the process scan found nothing to kill.'''
    if not user_data_folder:
        return
    for candidate in (Path(user_data_folder) / 'EBWebView' / 'lockfile',
                      Path(user_data_folder) / 'lockfile'):
        try:
            if candidate.exists():
                candidate.unlink()
                diag.log('app', f'removed stale WebView2 profile lockfile: {candidate}', 'warning')
        except Exception:
            pass


def _cleanupstalewebviewprocesses(user_data_folder: str) -> int:
    '''Kill orphaned msedgewebview2.exe processes left by a previously force-killed
    instance. They deadlock webview.start() on the next launch (window appears but the
    app hangs). We match by our WebView2 data-folder path (set via
    WEBVIEW2_USER_DATA_FOLDER) so we only kill THIS app's webviews — other WebView2
    apps (e.g. VS Code) are never touched, and the kill never blocks for 20s.
    When nothing was killed, also drop a stale profile lockfile (see
    _releasestaleprofilelock).'''
    n = _kill_procs_by_cmdline('msedgewebview2.exe', user_data_folder)
    if n == 0:
        n = _kill_procs_by_cmdline('msedgewebview2', user_data_folder)
    if n == 0:
        _releasestaleprofilelock(user_data_folder)
    return n


'''main'''

# The UI runs as a supervised child process. The child writes this flag file once the
# WebView2 page has actually loaded; the supervisor watches it to detect a hung init.
WV_LOADED_FLAG_ENV = 'VD_WV_LOADED_FLAG'
MAX_UI_ATTEMPTS = 5
# A healthy page measures 2-10s warm, but a *cold* first launch (Defender scanning
# the freshly built files + WebView2 shader compilation / profile conversion,
# measured 16-60s in the field with a ~2.3GB GPU-memory spike) can legitimately
# exceed 30s. Killing attempt 1 at 30s wasted exactly 30s + a full restart on
# every cold start, hence the generous early budget below.
UI_LOAD_TIMEOUT = 30
# the first TWO attempts get the generous budget: on a cold machine the profile
# conversion + Defender scan can span a restart (measured: attempt 1 failed at
# 60s, attempt 2 was warm-enough to load in 2s but a 30s budget killed it first)
UI_LOAD_TIMEOUT_EARLY = 60
REGEN_ATTEMPTS_EARLY_TIMEOUT = 2


def _webview_procs_alive(user_data_folder: str, sample_secs: float = 3.0) -> bool:
    '''True when at least one of THIS app's WebView2 processes exists AND is doing
    real work (non-zero CPU over a short sample). A poisoned init either never
    spawns the browser process or leaves it fully idle �� both are worth retrying
    immediately instead of burning the whole load timeout. On any probe error we
    assume "alive" and fall back to the full timeout (safe default).'''
    try:
        import psutil

        def snapshot():
            found = []
            for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if p.info['name'] and 'msedgewebview2' in p.info['name'].lower():
                        cmd = ' '.join(p.info['cmdline'] or '')
                        if user_data_folder.lower() in cmd.lower():
                            found.append(p)
                except Exception:
                    continue
            return found

        procs = snapshot()
        if not procs:
            return False
        cpu1 = {}
        for p in procs:
            try:
                cpu1[p.pid] = sum(p.cpu_times())
            except Exception:
                pass
        time.sleep(sample_secs)
        for p in snapshot():
            try:
                if sum(p.cpu_times()) > cpu1.get(p.pid, 0.0) + 0.01:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return True


def _wait_for_webview_loaded(flag_path: str, timeout: float, wv_folder: str = '') -> bool:
    '''Poll for the child's "page loaded" flag file. Returns True once it appears.

    Adds a liveness checkpoint ~20s in: if by then no WebView2 process of ours is
    alive-and-busy, the init is hung �� return False early so the supervisor
    retries right away instead of waiting out the full (possibly 60s) budget.
    A healthy page loads in 2-10s warm and shows busy browser processes on a
    cold start, so the probe never fires for a legitimate launch.'''
    started = time.monotonic()
    deadline = started + timeout
    probe_at = started + min(20.0, max(10.0, timeout / 3.0))
    dead_probes = 0
    while time.monotonic() < deadline:
        if os.path.exists(flag_path):
            return True
        if wv_folder and dead_probes < 2 and time.monotonic() >= probe_at:
            if not _webview_procs_alive(wv_folder):
                dead_probes += 1
                diag.log('app', f'supervisor: liveness probe {dead_probes}/2 found no busy WebView2 '
                                f'process ({time.monotonic() - started:.0f}s in)')
                probe_at = time.monotonic() + 10.0
                if dead_probes >= 2:
                    diag.log('app', 'supervisor: two consecutive dead liveness probes; '
                                    'init is hung, retrying early', 'warning')
                    return False
            else:
                # once proven alive, stop probing: the flag file decides the outcome
                wv_folder = ''
                diag.log('app', f'supervisor: WebView2 process alive at liveness checkpoint '
                                f'({time.monotonic() - started:.0f}s in); continuing to wait')
        time.sleep(0.25)
    return False


def _kill_process_tree(pid: int) -> None:
    '''Kill a process and everything it spawned (its msedgewebview2.exe children).

    Uses psutil directly instead of `taskkill /f /T /pid` because that command can
    hang for the full 20s timeout when the child's webview processes are stuck, which
    left the hung child (and its orphans) alive and forced the whole 5-attempt retry
    cycle on every launch.'''
    try:
        import psutil
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass
    except Exception as err:
        diag.log('app', f'_kill_process_tree({pid}) failed: {err}', 'debug')


def _hard_exit() -> None:
    '''Forcefully terminate THIS process and its entire child tree. Used as the
    final guarantee on window close so nothing is left running in the background.

    The UI child hosts both the WebView2 runtime (msedgewebview2.exe) AND every
    engine subprocess (ffmpeg / aria2c / node / DrissionPage browser). If any of
    those is still alive when the window closes — e.g. a download was cancelled
    mid-flight, or a worker thread is blocked — the python process would never
    exit on its own and they would all linger. Killing the whole tree here makes
    the "close window == process gone" contract actually hold.'''
    try:
        me = psutil.Process(os.getpid())
        for child in me.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
    except Exception as err:
        diag.log('app', f'_hard_exit: child kill failed: {err}', 'debug')
    os._exit(0)


def run_ui(args) -> int:
    '''The actual UI process. Run as a supervised child (--child). The single-instance
    lock is owned by the supervisor, so we don't re-acquire it here.'''
    with diag.step('app', 'setup stdio'):
        setupstdio()
    diag.install_excepthooks()

    with diag.step('app', 'setup bundled tools PATH'):
        setupbundledtools()

    with diag.step('app', 'prepare webview2 data folder'):
        setupwebviewdatafolder()
        _wv_folder = os.environ.get('WEBVIEW2_USER_DATA_FOLDER')
        if _wv_folder:
            _n = _cleanupstalewebviewprocesses(_wv_folder)
            if _n:
                diag.log('app', f'killed {_n} stale WebView2 process(es) from a previous force-killed instance '
                                f'(prevents webview.start deadlock); waiting for the OS to release the lock', 'warning')
                time.sleep(1.5)

    with diag.step('app', 'import webview + backend.api'):
        try:
            import webview
            from backend.api import JsApi
        except Exception as err:
            diag.log('app', f'failed to import webview/backend: {err}', 'error')
            diag.log('app', traceback.format_exc(), 'debug')
            return 1
    diag.log('app', f'pywebview runtime: {getattr(webview, "__version__", "?")}, guis: {webview.guis if hasattr(webview, "guis") else "?"}')

    api = JsApi(version=APP_VERSION)
    index_file = resourcepath('web') / 'index.html'
    if not index_file.exists():
        diag.log('app', f'frontend not found: {index_file}', 'error')
        return 1
    diag.log('app', f'frontend file: {index_file} exists={index_file.exists()}')
    url = index_file.as_uri()
    if args.url:
        url = f'{url}#{args.url}'

    # When supervised, start HIDDEN and only show the window once the page has
    # actually loaded. A hung WebView2 init then never shows a frozen window —
    # the supervisor just kills the invisible child and retries, so the user
    # never sees the "frozen on first open" freeze, only a slightly later window.
    _start_hidden = os.environ.get('VD_UI_START_HIDDEN') == '1'

    with diag.step('app', 'create window'):
        window = webview.create_window(
            title=f'{APP_NAME} v{APP_VERSION}', url=url, js_api=api,
            width=1240, height=820, min_size=(980, 680), background_color='#0b0e14',
            text_select=True, confirm_close=False, hidden=_start_hidden,
        )
        api.bindwindow(window)
        diag.log('app', f'window created: hidden={_start_hidden} events={type(window.events.loaded).__name__}')

    loaded_flag = threading.Event()
    _window_shown = threading.Event()
    _flag_path = os.environ.get(WV_LOADED_FLAG_ENV, '')

    def _showwindowwhenready(attempt: int = 0):
        if _window_shown.is_set():
            return
        try:
            window.show()
            _window_shown.set()
            diag.log('app', 'window shown after successful load')
        except Exception as err:
            if attempt < 5:
                threading.Timer(1.0, _showwindowwhenready, args=(attempt + 1,)).start()
            else:
                diag.log('app', f'window.show() failed after retries: {err}', 'error')

    def _showwindowearly(attempt: int = 0):
        '''Feedback fallback: if the page has not finished loading 3s in, show the
        (dark, still-loading) window anyway. On a cold start the WebView2 init can
        take 15s+, and a fully invisible app makes the user double-click again —
        which used to kill the startup-in-progress.

        IMPORTANT: this MUST go through the native ShowWindow, NOT pywebview's
        window.show(). The latter issues a synchronous Invoke onto the UI thread,
        which deadlocks the WebView2 initialization (measured: every attempt whose
        window.show() ran during init NEVER fired events.loaded and got killed by
        the supervisor; every attempt without it loaded in 2-3s). A raw
        ShowWindow(hwnd) does not touch pywebview's event pipeline at all.'''
        if _window_shown.is_set() or loaded_flag.is_set():
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, f'{APP_NAME} v{APP_VERSION}')
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
                _window_shown.set()
                diag.log('app', 'window shown early via native ShowWindow (page still loading) '
                                'so the user gets immediate feedback')
            elif attempt < 10:
                # native form not created yet; retry shortly
                threading.Timer(0.5, _showwindowearly, args=(attempt + 1,)).start()
            else:
                diag.log('app', 'early-show gave up: native window not found')
        except Exception as err:
            diag.log('app', f'early window.show() failed: {err}', 'debug')

    if _start_hidden:
        threading.Timer(3.0, _showwindowearly).start()

    def onloaded():
        try:
            loaded_flag.set()
            diag.log('app', 'webview page loaded')
            # started hidden (supervised): NOW the page is really alive — show it
            _showwindowwhenready()
            # tell the supervisor (if any) that the UI is genuinely alive
            if _flag_path:
                try:
                    Path(_flag_path).write_text(str(os.getpid()), encoding='utf-8')
                except Exception:
                    pass
            if args.url:
                window.evaluate_js(f'window.__prefillUrl && window.__prefillUrl({args.url!r})')
        except Exception as err:
            diag.log('app', f'onloaded handler failed: {err}', 'error')
            diag.log('app', traceback.format_exc(), 'debug')

    window.events.loaded += onloaded

    def _on_closed():
        '''Window was closed: cancel downloads, kill child processes, and make
        absolutely sure the process (and its WebView2 / engine children) exits.

        Without this, a still-running download keeps the non-daemon executor
        thread alive and the ffmpeg/aria2c/node children (and msedgewebview2.exe)
        keep running in the background after the window is gone.'''
        diag.log('app', 'window closed; cancelling jobs and terminating background processes')
        try:
            api.shutdown()
        except Exception as err:
            diag.log('app', f'_on_closed: api.shutdown failed: {err}', 'debug')
        _hard_exit()

    window.events.closed += _on_closed

    def watchdog():
        '''If the page never loads, the WebView2 init is dead-locked. The supervisor
        process is watching the loaded flag and will kill+restart us; here we only log.'''
        if loaded_flag.wait(UI_LOAD_TIMEOUT):
            diag.log('app', 'watchdog: page loaded in time')
            return
        diag.log('app', f'watchdog: page did NOT load within {UI_LOAD_TIMEOUT}s; WebView2 environment is stuck. '
                        f'A supervisor should restart this process automatically.', 'warning')

    threading.Thread(target=watchdog, name='startup-watchdog', daemon=True).start()

    diag.log('app', 'entering webview event loop (ui thread blocks here)')
    # Use a PERSISTENT profile in our dedicated WEBVIEW2_USER_DATA_FOLDER. A
    # private (in-memory) profile threw away the GPU shader cache on every
    # launch, which alone cost 10s+ on cold starts. Stale-profile lock deadlocks
    # — the reason private mode was introduced — are already handled by
    # _cleanupstalewebviewprocesses() in both supervisor and child. A persistent
    # profile also keeps webview-side logins alive between launches.
    webview.start(debug=bool(args.debug), private_mode=False, gui='edgechromium')
    diag.log('app', 'webview event loop exited, application is shutting down')
    return 0


def run_supervisor(args) -> int:
    '''Launch the UI as a child process and supervise it. If the WebView2 page fails to
    load within `UI_LOAD_TIMEOUT` (the classic "window appears but the app is frozen"
    deadlock), kill the child (and its msedgewebview2.exe tree) and retry — up to
    MAX_UI_ATTEMPTS times. This guarantees the app never stays frozen on a hung WebView2
    init: at worst it spends ~30s and then recovers on its own.'''
    with diag.step('app', 'setup stdio'):
        setupstdio()
    diag.install_excepthooks()

    if not acquiresingleinstance():
        return 0

    # Kill orphaned WebView2 processes from any previously force-killed instance
    # BEFORE launching the first child, so attempt 1 starts in a clean environment
    # instead of being sacrificed to a deadlocked init and then silently retried.
    try:
        from platformdirs import user_data_dir
        _wv_folder = str(Path(user_data_dir(appname='vd-desktop', appauthor='vd')) / 'webview2')
    except Exception:
        _wv_folder = str(Path.home() / '.vd-desktop' / 'webview2')
    os.environ.setdefault('WEBVIEW2_USER_DATA_FOLDER', _wv_folder)
    _n = _cleanupstalewebviewprocesses(_wv_folder)
    if _n:
        diag.log('app', f'killed {_n} stale WebView2 process(es) before first launch; '
                        f'waiting for the OS to release the lock', 'warning')
        time.sleep(1.5)

    # a unique flag file for this supervisor session; the UI child writes to it on load
    flag_fd, flag_path = tempfile.mkstemp(prefix='vd_wv_loaded_', suffix='.flag')
    os.close(flag_fd)

    # forward the original args to the child, stripping our internal --child marker.
    # Frozen: the exe is self-bootstrapping, so [exe, ...args] is enough. In dev the
    # interpreter needs the script path explicitly, otherwise `python --child` just
    # errors out silently and the supervisor burns its 5 attempts on nothing.
    if getattr(sys, 'frozen', False):
        child_args = [sys.executable] + [a for a in sys.argv[1:] if a != '--child'] + ['--child']
    else:
        child_args = [sys.executable, str(Path(sys.argv[0]).resolve())] + \
            [a for a in sys.argv[1:] if a != '--child'] + ['--child']

    for attempt in range(1, MAX_UI_ATTEMPTS + 1):
        try:
            os.remove(flag_path)
        except OSError:
            pass
        diag.log('app', f'supervisor: launching UI child (attempt {attempt}/{MAX_UI_ATTEMPTS})')
        env = dict(os.environ)
        env[WV_LOADED_FLAG_ENV] = flag_path
        # every attempt starts hidden and only shows after a real page load (or
        # after 3s as feedback), so a hung attempt never surfaces as a frozen
        # window before the user gets anything to look at
        env['VD_UI_START_HIDDEN'] = '1'
        try:
            proc = subprocess.Popen(child_args, env=env)
        except Exception as err:
            diag.log('app', f'supervisor: failed to launch UI child: {err}', 'error')
            return 1
        _timeout = UI_LOAD_TIMEOUT_EARLY if attempt <= REGEN_ATTEMPTS_EARLY_TIMEOUT else UI_LOAD_TIMEOUT
        loaded = _wait_for_webview_loaded(flag_path, _timeout, _wv_folder)
        if loaded:
            diag.log('app', 'supervisor: UI loaded OK; attaching to child until it exits')
            try:
                proc.wait()
            except Exception:
                pass
            try:
                os.remove(flag_path)
            except OSError:
                pass
            # final guarantee on "close == every process gone": reap any WebView2
            # orphans the child may have left behind (no-op after a clean exit)
            _n = _cleanupstalewebviewprocesses(_wv_folder)
            if _n:
                diag.log('app', f'supervisor: reaped {_n} leftover WebView2 process(es) after child exit', 'warning')
            diag.log('app', 'shutdown complete: UI child exited, no app-owned processes remain')
            return 0
        # Hung: kill the child and its webview2 tree, then retry.
        diag.log('app', f'supervisor: UI did not load within {_timeout}s (attempt {attempt}/{MAX_UI_ATTEMPTS}); '
                        f'terminating the hung child and retrying', 'warning')
        _kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        time.sleep(1.0)

    diag.log('app', f'supervisor: all {MAX_UI_ATTEMPTS} attempts failed; giving up. '
                    f'WebView2 cannot initialize on this machine — try rebooting or repairing the '
                    f'Microsoft Edge WebView2 Runtime.', 'error')
    try:
        os.remove(flag_path)
    except OSError:
        pass
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=f'{APP_NAME} v{APP_VERSION}')
    parser.add_argument('--debug', action='store_true', help='enable the web inspector and keep the console output')
    parser.add_argument('--url', default=None, help='prefill the url input box')
    parser.add_argument('--selftest', nargs='?', const=SELFTEST_URL, default=None,
                        help='run a headless parse+download self test and write a json report to the user home dir')
    parser.add_argument('--child', action='store_true', dest='child',
                        help=argparse.SUPPRESS)  # internal: run the UI as a supervised child
    args = parser.parse_args()

    diag.log('app', f'===== launch: {APP_NAME} v{APP_VERSION} args={sys.argv[1:]} frozen={bool(getattr(sys, "frozen", False))} python={sys.version.split()[0]} exe={getattr(sys, "executable", "?")} =====')

    if args.selftest:
        diag.log('app', f'selftest requested: {args.selftest}')
        return runselftest(args.selftest)

    if args.child:
        return run_ui(args)

    return run_supervisor(args)


if __name__ == '__main__':
    raise SystemExit(main())
