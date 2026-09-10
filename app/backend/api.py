'''
Function:
    The JS bridge exposed to the webview frontend (`window.pywebview.api.*`)
Author:
    CodeBuddy
'''
from __future__ import annotations

import threading
from typing import Any, Dict, List

from . import diag
from .core import VideoDlService, defaultworkdir, HistoryStore, Config

try:  # webview is only available when running the desktop shell
    import webview
except Exception:  # pragma: no cover
    webview = None


'''JsApi'''


class JsApi():
    def __init__(self, service: VideoDlService = None, version: str = '1.0.0'):
        # IMPORTANT: every attribute here is deliberately underscore-prefixed.
        # pywebview builds the `window.pywebview.api` proxy by RECURSIVELY walking
        # the js_api object (webview/util.py::inject_pywebview -> get_functions),
        # and `name.startswith('_')` is the only thing that stops it. With plain
        # `self.service` / `self._window` it walked the whole object graph —
        # VideoDlService (config, thread pool, progress bus, builder classes) and
        # the pywebview Window itself — allocating hundreds of thousands of python
        # objects: the proxy took 30-60s to build and rss grew to 1.7-4.8GB on a
        # cold start. Private names keep the walk down to ~30 plain methods.
        self._service = service or VideoDlService(version=version)
        self._version = version
        self._window = None

    '''bind the pywebview window (used by the folder dialog)'''
    def bindwindow(self, window) -> None:
        self._window = window

    '''-------------------- bootstrap --------------------'''

    def bootstrap(self) -> Dict[str, Any]:
        return {
            'version': self._version,
            'config': {
                'work_dir': self._service.config.work_dir,
                'num_threadings': self._service.config.num_threadings,
                'concurrent_downloads': self._service.config.concurrent_downloads,
                'proxy': self._service.config.proxy,
                'cookies': self._service.config.cookies,
                'per_source_cookies': self._service.config.per_source_cookies,
                'default_quality': self._service.config.default_quality,
                'apply_common_clients_only': self._service.config.apply_common_clients_only,
                'download_subtitles': self._service.config.download_subtitles,
                'allowed_sources': self._service.config.allowed_sources,
                'last_url': self._service.config.last_url,
                'language': self._service.config.language,
            },
            'default_work_dir': defaultworkdir(),
            'engine_ready': self._service.engineready,
            'engine_state': self._service.engine_state,
            'engine_version': self._service.engine_version,
        }

    def sources(self) -> Dict[str, Any]:
        # do NOT trigger a load here; the engine is loaded on demand (first parse).
        # In lazy mode (`VD_LAZY_PARSERS=1`), `source_names` only contains the
        # parsers that have been imported via URL matching — so if the user has
        # only parsed bilibili/抖音, the settings UI would only show those two
        # parsers. Use the parser-name tables (populated by `ensureengine`) as
        # the authoritative list so the user can configure ANY of the ~60+
        # available parsers, not just the few that have been touched.
        if not self._service.engineready:
            # Kick off a BACKGROUND load only. We must NOT use wait=True here:
            # `sources()` is called from the frontend polling loop, and a
            # synchronous load would block this API call (and the pywebview UI
            # thread that invoked it) for ~10s on first launch — the user
            # perceives this as a hang/freeze at startup. The frontend detects
            # the engine-ready transition (prevEngineReady) and re-fetches the
            # full ~60+ parser list at that point, so we don't need to block here.
            try:
                self._service.ensureengine(wait=False)
            except Exception:
                pass

        platforms: List[str] = []
        generic: List[str] = []
        if self._service.engineready:
            platform_table = getattr(self._service, '_platform_parser_table', []) or []
            common_table = getattr(self._service, '_common_parser_table', []) or []
            # Filter out abstract / internal base classes that shouldn't be
            # exposed to the user (e.g. BaseVideoClient itself).
            _internal = {'BaseVideoClient', 'CommonVideoClient', 'BaseModuleBuilder'}
            platforms = [cls for _, cls in platform_table if cls not in _internal]
            generic = [cls for _, cls in common_table if cls not in _internal]
        else:
            # engine still loading: fall back to whatever has registered so far
            platforms = list(self._service.source_names or [])
            generic = list(self._service.common_source_names or [])

        # defensive fallback: if the tables are empty but the engine claims
        # to be ready, also pull from the live REGISTERED_MODULES so the UI
        # chip can show the real count.
        if self._service.engineready and (not platforms or not generic):
            try:
                from vd.modules import VideoClientBuilder, CommonVideoClientBuilder  # noqa: WPS433
                if not platforms:
                    platforms = sorted(VideoClientBuilder.REGISTERED_MODULES.keys())
                if not generic:
                    generic = sorted(CommonVideoClientBuilder.REGISTERED_MODULES.keys())
            except Exception:
                pass

        return {
            'engine_ready': self._service.engineready,
            'engine_state': self._service.engine_state,
            'engine_error': self._service.engineerror,
            'platforms': platforms,
            'generic': generic,
        }

    '''-------------------- config --------------------'''

    def setconfig(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        config = config or {}
        cfg = self._service.config
        if 'work_dir' in config and str(config['work_dir']).strip():
            cfg.work_dir = str(config['work_dir']).strip()
        if 'num_threadings' in config:
            try:
                cfg.num_threadings = max(1, min(32, int(config['num_threadings'])))
            except Exception:
                pass
        if 'concurrent_downloads' in config:
            try:
                cfg.concurrent_downloads = max(1, min(16, int(config['concurrent_downloads'])))
            except Exception:
                pass
        if 'proxy' in config:
            cfg.proxy = str(config['proxy'] or '').strip()
        if 'cookies' in config:
            cfg.cookies = str(config['cookies'] or '').strip()
        if 'per_source_cookies' in config and isinstance(config['per_source_cookies'], dict):
            cfg.per_source_cookies = dict(config['per_source_cookies'] or {})
        if 'default_quality' in config:
            cfg.default_quality = str(config['default_quality'] or 'best').strip().lower() or 'best'
        if 'apply_common_clients_only' in config:
            cfg.apply_common_clients_only = bool(config['apply_common_clients_only'])
        if 'download_subtitles' in config:
            cfg.download_subtitles = bool(config['download_subtitles'])
        if 'allowed_sources' in config:
            value = config['allowed_sources']
            cfg.allowed_sources = list(value) if isinstance(value, (list, tuple)) else []
        if 'language' in config:
            value = str(config['language'] or '').strip()
            cfg.language = value if value in {'zh-CN', 'en-US'} else ''
        cfg.save()
        # Rebuilding the client is expensive (several seconds on a cold engine): it
        # MUST NOT run on the bridge thread that called setconfig, or the 保存
        # button freezes for that long. Rebuild on a background thread instead.
        threading.Thread(target=self._rebuild_client, name='rebuild-client', daemon=True).start()
        self._service.log('info', 'settings saved')
        return {'ok': True, 'config': self.bootstrap()['config']}

    def _rebuild_client(self) -> None:
        try:
            self._service._buildclient(force=True)
        except Exception as err:
            diag.log('api', f'client rebuild failed: {err}', 'warning')

    def pickfolder(self) -> Dict[str, Any]:
        if self._window is None:
            return {'ok': False, 'error': 'the window is not ready yet'}
        try:
            folder_flag = getattr(getattr(webview, 'FileDialog', None), 'FOLDER', getattr(webview, 'FOLDER_DIALOG', None))
            result = self._window.create_file_dialog(folder_flag, directory=self._service.config.work_dir or defaultworkdir())
        except Exception as err:
            return {'ok': False, 'error': str(err)}
        if not result:
            return {'ok': False, 'cancelled': True}
        path = result[0] if isinstance(result, (list, tuple)) else str(result)
        return {'ok': True, 'path': path}

    '''-------------------- parse / download --------------------'''

    def parse(self, url: str) -> Dict[str, Any]:
        return self._run_async_parse('parse', url)

    def parsebatch(self, urls: List[str] = None) -> Dict[str, Any]:
        urls = [str(u).strip() for u in (urls or []) if str(u).strip()]
        if not urls:
            return {'ok': False, 'error': '没有提供链接', 'items': []}
        return self._run_async_parse('parse_batch', urls)

    def _run_async_parse(self, method: str, arg) -> Dict[str, Any]:
        # One shared implementation for parse()/parsebatch(): run the network-bound
        # parse on a background thread so the pywebview bridge thread is never
        # blocked by a slow/stalled parser. The result is pushed to the frontend via
        # window.applyParseResult(); this call returns immediately with an async
        # token. That is what keeps "解析" responsive even on a bad network.
        import json, uuid
        if getattr(self, '_parse_jobs', None) is None:
            self._parse_jobs = {}
        token = uuid.uuid4().hex
        self._parse_jobs[token] = None

        def _run():
            try:
                result = self._service.parse(arg) if method == 'parse' else self._service.parse_batch(arg)
            except Exception:
                import traceback as _tb
                diag.log('core', f'{method} thread crashed: {_tb.format_exc()}', 'error')
                result = {'ok': False, 'error': f'{method} 线程异常', 'items': []}
                if method == 'parse_batch':
                    result['batch'] = True
                    result['url_count'] = len(arg) if isinstance(arg, list) else 0
            self._parse_jobs[token] = result
            try:
                if self._window is not None:
                    payload = json.dumps({'token': token, 'result': result}, ensure_ascii=False)
                    self._window.evaluate_js(f'(window.applyParseResult||function(){{}})({payload})')
            except Exception as err:
                diag.log('core', f'failed to push parse result to frontend: {err}', 'warning')

        threading.Thread(target=_run, name=f'{method}-{token[:8]}', daemon=True).start()
        return {'ok': True, 'async': True, 'token': token}

    def download(self, keys: List[str] = None, work_dir: str = None) -> Dict[str, Any]:
        return self._service.enqueue(list(keys or []), work_dir)

    def pause(self, job_id: str) -> Dict[str, Any]:
        return self._service.pause(job_id)

    def resume(self, job_id: str) -> Dict[str, Any]:
        return self._service.resume(job_id)

    def cancel(self, job_id: str) -> Dict[str, Any]:
        return self._service.cancel(job_id)

    def retryaudio(self, job_id: str) -> Dict[str, Any]:
        return self._service.retry_audio(job_id)

    def clearjobs(self) -> Dict[str, Any]:
        return self._service.clearjobs()

    def shutdown(self) -> Dict[str, Any]:
        '''Cancel all downloads and kill child processes. Invoked when the UI
        window is closed so the app does not leave orphaned ffmpeg/aria2c/node
        or WebView2 processes running in the background.'''
        try:
            self._service.shutdown()
        except Exception as err:
            diag.log('api', f'shutdown failed: {err}', 'warning')
        return {'ok': True}

    def state(self, after_seq: int = 0) -> Dict[str, Any]:
        try:
            after_seq = int(after_seq or 0)
        except Exception:
            after_seq = 0
        return self._service.state(after_seq)

    '''frontend log sink: javascript anchors are forwarded into startup.log'''

    def felog(self, scope: str = 'ui', message: str = '', level: str = 'info') -> Dict[str, Any]:
        try:
            diag.log(str(scope or 'ui')[:20], str(message or '')[:500], str(level or 'info')[:8])
        except Exception:
            pass
        return {'ok': True}

    '''-------------------- account login (in-app browser) --------------------'''

    def login(self, source: str = '') -> Dict[str, Any]:
        '''Open the in-app login window (DrissionPage browser) for `source`.'''
        from .login import login_manager
        if not source:
            return {'ok': False, 'error': '缺少平台参数'}
        return login_manager.start(source, self._service)

    def login_finish(self, source: str = '') -> Dict[str, Any]:
        '''Tell the running login window the user has finished signing in.'''
        from .login import login_manager
        if not source:
            return {'ok': False, 'error': '缺少平台参数'}
        return login_manager.finish(source)

    def login_status(self) -> Dict[str, Any]:
        '''Return the live login state of every source seen so far.'''
        from .login import login_manager, LOGIN_URLS
        return {
            'logins': login_manager.status(),
            'per_source_cookies': dict(self._service.config.per_source_cookies or {}),
            'supported': list(LOGIN_URLS.keys()),
        }

    def logout(self, source: str = '') -> Dict[str, Any]:
        '''Forget the stored login cookie for `source`.'''
        from .login import login_manager
        if not source:
            return {'ok': False, 'error': '缺少平台参数'}
        return login_manager.logout(source, self._service)

    '''-------------------- system helpers --------------------'''

    def openpath(self, path: str) -> Dict[str, Any]:
        return VideoDlService.openpath(path)

    def revealpath(self, path: str) -> Dict[str, Any]:
        '''打开文件所在目录并选中该文件。'''
        return VideoDlService.revealpath(path)

    def openurl(self, url: str) -> Dict[str, Any]:
        return VideoDlService.openurl(url)

    def openconfigdir(self) -> Dict[str, Any]:
        '''Reveal the directory that stores the desktop config json.'''
        return VideoDlService.openpath(str(Config.configpath().parent))

    def history(self) -> Dict[str, Any]:
        return {'history': list(self._service.history)}

    def clearhistory(self) -> Dict[str, Any]:
        self._service.history = HistoryStore.clear()
        return {'ok': True, 'history': self._service.history}

    def removehistory(self, url: str) -> Dict[str, Any]:
        self._service.history = HistoryStore.remove(url or '')
        return {'ok': True, 'history': self._service.history}

    def checktools(self) -> Dict[str, Any]:
        import shutil
        return {
            'ffmpeg': bool(shutil.which('ffmpeg')),
            'ffprobe': bool(shutil.which('ffprobe')),
            'node': bool(shutil.which('node')),
            'nm3u8dlre': bool(shutil.which('N_m3u8DL-RE')),
            'aria2c': bool(shutil.which('aria2c')),
        }
