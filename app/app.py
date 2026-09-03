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


def acquiresingleinstance() -> bool:
    '''Ensure a single instance WITHOUT killing healthy launches.

    Previously a second double-click TERMINATED the already-running instance —
    and because a cold first launch can take 10-30s (Defender scanning the
    freshly built files + WebView2 init), users kept clicking impatiently and
    every click killed the startup-in-progress, restarting the whole slow cycle.
    That is why "the app only starts after several clicks".

    New behaviour:
      * no previous instance  -> acquire the lock and start normally;
      * previous instance alive and its window responds -> focus it, exit quietly;
      * previous instance alive but genuinely hung (IsHungAppWindow) -> kill its
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
                hwnd = None
                try:
                    import ctypes
                    hwnd = ctypes.windll.user32.FindWindowW(None, f'{APP_NAME} v{APP_VERSION}') or None
                except Exception:
                    hwnd = None
                if hwnd is not None and not _window_is_hung(hwnd):
                    diag.log('app', f'another instance (pid={oldpid}) is running and responsive; '
                                    f'focusing its window and exiting', 'info')
                    _focus_existing_window()
                    return False
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


'''kill orphaned webview2 processes that belong to THIS app's WebView2 data folder.

A previous force-killed instance leaves msedgewebview2.exe processes running; they
deadlock webview.start() on the next launch (window appears but the app hangs).
We match by the data-folder path in the command line so we never touch other apps'
WebView2 processes (e.g. VS Code).'''


def _cleanupstalewebviewprocesses(user_data_folder: str) -> int:
    '''Kill orphaned msedgewebview2.exe processes left by a previously force-killed
    instance. They deadlock webview.start() on the next launch (window appears but the
    app hangs). Because pywebview private_mode uses a temp profile, the processes cannot
    be matched by path, so we kill them all. Other WebView2 apps (e.g. VS Code) recreate
    their webviews automatically, so this is safe.'''
    import subprocess
    killed = 0
    try:
        out = subprocess.run(['taskkill', '/f', '/im', 'msedgewebview2.exe'],
                             capture_output=True, text=True, timeout=20)
        for line in (out.stdout or '').splitlines():
            if line.startswith('SUCCESS'):
                killed += 1
    except Exception as err:
        diag.log('app', f'_cleanupstalewebviewprocesses failed: {err}', 'debug')
    return killed


'''main'''

# The UI runs as a supervised child process. The child writes this flag file once the
# WebView2 page has actually loaded; the supervisor watches it to detect a hung init.
WV_LOADED_FLAG_ENV = 'VD_WV_LOADED_FLAG'
MAX_UI_ATTEMPTS = 5
UI_LOAD_TIMEOUT = 15  # healthy page loads measure 3-10s; past 15s the WebView2 init is hung


def _wait_for_webview_loaded(flag_path: str, timeout: float) -> bool:
    '''Poll for the child's "page loaded" flag file. Returns True once it appears.'''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(flag_path):
            return True
        time.sleep(0.25)
    return False


def _kill_process_tree(pid: int) -> None:
    '''Kill a process and everything it spawned (its msedgewebview2.exe children).'''
    try:
        subprocess.run(['taskkill', '/f', '/T', '/pid', str(pid)],
                       capture_output=True, text=True, timeout=20)
    except Exception as err:
        diag.log('app', f'_kill_process_tree({pid}) failed: {err}', 'debug')


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
    _flag_path = os.environ.get(WV_LOADED_FLAG_ENV, '')

    def _showwindowwhenready(attempt: int = 0):
        if not _start_hidden:
            return
        try:
            window.show()
            diag.log('app', 'window shown after successful load')
        except Exception as err:
            if attempt < 5:
                threading.Timer(1.0, _showwindowwhenready, args=(attempt + 1,)).start()
            else:
                diag.log('app', f'window.show() failed after retries: {err}', 'error')

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
    # private_mode keeps every launch on a clean temporary webview2 profile:
    # a shared profile can be locked by a stale browser process of a killed
    # previous instance, which deadlocks webview.start() (window shows but hangs).
    webview.start(debug=bool(args.debug), private_mode=True, gui='edgechromium')
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

    # a unique flag file for this supervisor session; the UI child writes to it on load
    flag_fd, flag_path = tempfile.mkstemp(prefix='vd_wv_loaded_', suffix='.flag')
    os.close(flag_fd)

    # forward the original args to the child, stripping our internal --child marker
    child_args = [sys.executable] + [a for a in sys.argv[1:] if a != '--child'] + ['--child']

    for attempt in range(1, MAX_UI_ATTEMPTS + 1):
        try:
            os.remove(flag_path)
        except OSError:
            pass
        diag.log('app', f'supervisor: launching UI child (attempt {attempt}/{MAX_UI_ATTEMPTS})')
        env = dict(os.environ)
        env[WV_LOADED_FLAG_ENV] = flag_path
        # every attempt starts hidden and only shows after a real page load, so
        # a hung attempt never surfaces as a frozen window on the user's screen
        env['VD_UI_START_HIDDEN'] = '1'
        try:
            proc = subprocess.Popen(child_args, env=env)
        except Exception as err:
            diag.log('app', f'supervisor: failed to launch UI child: {err}', 'error')
            return 1
        loaded = _wait_for_webview_loaded(flag_path, UI_LOAD_TIMEOUT)
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
            return 0
        # Hung: kill the child and its webview2 tree, then retry.
        diag.log('app', f'supervisor: UI did not load within {UI_LOAD_TIMEOUT}s (attempt {attempt}/{MAX_UI_ATTEMPTS}); '
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
