'''
Function:
    Desktop backend core service
    - lazily imports the vd engine (it is heavy: 60+ platform parsers)
    - keeps the original `VideoInfo` objects alive so that no download capability is lost
    - runs downloads in a background worker and exposes plain dicts to the webview frontend
Author:
    CodeBuddy
'''
from __future__ import annotations

import os
import re
import sys
import json
import queue
import logging
import threading
import traceback
import subprocess
import time
import importlib
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from . import diag
from .progress import ProgressBus, install_progress_hook, DownloadCancelled

# Enable the lazy-parser mode we wired into the vendored vd upstream.
# Each `vd.modules.sources.<x>` parser is imported on first need, and its
# `XxxVideoClient` class self-registers into `VideoClientBuilder.REGISTERED_MODULES`
# via the `AutoRegisterMeta` metaclass on `BaseVideoClient`. The vd
# command-line tool runs without this env var and falls back to the original
# eager-import behaviour, so this change is desktop-only.
os.environ.setdefault('VD_LAZY_PARSERS', '1')

# Windows: the packaged exe is a windowed app, but every console-subsystem child
# (ffmpeg merging video+audio, N_m3u8DL-RE, aria2c, node) still pops up its own
# black console window. Patch subprocess.Popen once, process-wide, so any child
# spawned without explicit flags gets CREATE_NO_WINDOW — no more console flashes.
if os.name == 'nt':
    import subprocess as _subprocess
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen_init = _subprocess.Popen.__init__

    def _no_window_popen_init(self, *args, **kwargs):
        kwargs.setdefault('creationflags', _CREATE_NO_WINDOW)
        _orig_popen_init(self, *args, **kwargs)

    _subprocess.Popen.__init__ = _no_window_popen_init


'''paths'''
DESKTOP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DESKTOP_ROOT.parent
VD_SRC = PROJECT_ROOT / 'engine'


'''DEFAULT_WORK_DIR'''


def defaultworkdir() -> str:
    videos = Path.home() / 'Videos'
    base = videos if videos.exists() else Path.home()
    return str(base / 'vd_downloads')


'''Config'''

# Platforms enabled out of the box. Keep this tiny so the first launch / first
# parse is fast (no 60+ parser modules imported up front). Users opt into more
# platforms from the Settings panel — each is lazy-loaded on first use.
# WebMediaGrabber always stays available as the universal fallback.
DEFAULT_ALLOWED_SOURCES = ['DouyinVideoClient', 'BilibiliVideoClient']


@dataclass
class Config():
    work_dir: str = field(default_factory=defaultworkdir)
    num_threadings: int = 5
    proxy: str = ''
    cookies: str = ''
    # per-source login cookies captured via the in-app login window (DrissionPage).
    # keyed by the vd source class name, e.g. 'BilibiliVideoClient'.
    per_source_cookies: Dict[str, str] = field(default_factory=dict)
    # global preferred quality used for the "default selected quality" behaviour:
    # 'best' | '4k' | '1080p' | '720p' | '480p' | '360p' | 'auto'
    default_quality: str = 'best'
    apply_common_clients_only: bool = False
    # Default whitelist: only 抖音 + bilibili load by default. Other platforms are
    # available on demand from Settings → check the ones you need. This keeps the
    # first launch fast; the lazy-import machinery (VD_LAZY_PARSERS=1) means
    # even these two are only imported when their URL is actually parsed.
    allowed_sources: List[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_SOURCES))
    last_url: str = ''

    '''config path'''

    @staticmethod
    def configpath() -> Path:
        try:
            from platformdirs import user_config_dir
            path = Path(user_config_dir(appname='vd-desktop', appauthor='vd'))
        except Exception:
            path = Path.home() / '.vd-desktop'
        path.mkdir(parents=True, exist_ok=True)
        return path / 'config.json'

    '''load'''

    # Hardcoded set of parser class names that have been removed from the engine.
    # Used to clean stale entries from user config at load time (before the engine
    # is initialized). The dynamic cleanup in ensureengine() catches any future
    # deletions automatically.
    _DELETED_PARSER_CLASSES = frozenset({
        # platform parsers removed earlier
        'PlayerPlVideoClient', 'WittyTVVideoClient', 'PlusFifaVideoClient',
        # common parsers removed (all 36 generic parsers)
        'APICXVideoClient', 'BVVideoClient', 'PVVideoClient', 'GVVideoClient',
        'RayVideoClient', 'VgetVideoClient', 'KIT9VideoClient', 'SpapiVideoClient',
        'XMFlvVideoClient', 'GVVIPVideoClient', 'KedouVideoClient', 'BugPkVideoClient',
        'MiZhiVideoClient', 'XCVTSVideoClient', 'ODwonVideoClient', 'WzjunVideoClient',
        'KuLeuVideoClient', 'SnapWCVideoClient', 'NoLogoVideoClient', 'IM1907VideoClient',
        'IIILabVideoClient', 'JXM3U8VideoClient', 'JisuYunVideoClient', 'VideoFKVideoClient',
        'LongZhuVideoClient', 'SnapAnyVideoClient', 'QingQiuVideoClient',
        'VThreadsVideoClient', 'VeedMateVideoClient', 'QingtingVideoClient',
        'KuKuToolVideoClient', 'SENJiexiVideoClient', 'ZanqianbaVideoClient',
        'XiaolvfangVideoClient', 'AnyFetcherVideoClient', 'QZXDPToolsVideoClient',
        'XiazaitoolVideoClient',
    })

    @classmethod
    def load(cls) -> 'Config':
        try:
            data = json.loads(cls.configpath().read_text(encoding='utf-8') or '{}')
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            # Clean stale entries for parsers that no longer exist. This runs
            # before engine init using the hardcoded deletion set; ensureengine()
            # does a second dynamic pass against the live parser tables.
            if 'allowed_sources' in known and isinstance(known['allowed_sources'], list):
                known['allowed_sources'] = [s for s in known['allowed_sources'] if s not in cls._DELETED_PARSER_CLASSES]
            if 'per_source_cookies' in known and isinstance(known['per_source_cookies'], dict):
                known['per_source_cookies'] = {k: v for k, v in known['per_source_cookies'].items() if k not in cls._DELETED_PARSER_CLASSES}
            # If the saved config has no whitelist (missing key OR empty list),
            # apply the 2-platform default (抖音+bilibili) instead of "load everything".
            # The old behaviour treated [] as "all parsers enabled", which flooded
            # the settings dialog with dozens of checkboxes.
            if 'allowed_sources' not in known or not known['allowed_sources']:
                known['allowed_sources'] = list(DEFAULT_ALLOWED_SOURCES)
            return cls(**known)
        except Exception:
            return cls()

    '''save'''

    def save(self) -> None:
        try:
            self.configpath().write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass


'''HistoryStore'''


class HistoryStore():
    '''Persisted list of urls the user has parsed, so they can be re-used later.'''
    MAX_ENTRIES = 200

    @staticmethod
    def historypath() -> Path:
        return Config.configpath().parent / 'history.json'

    @staticmethod
    def load() -> List[Dict[str, Any]]:
        try:
            data = json.loads(HistoryStore.historypath().read_text(encoding='utf-8') or '[]')
            return list(data) if isinstance(data, list) else []
        except Exception:
            return []

    @staticmethod
    def save(entries: List[Dict[str, Any]]) -> None:
        try:
            HistoryStore.historypath().write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    @staticmethod
    def add(url: str, title: str = '', source: str = '') -> List[Dict[str, Any]]:
        url = (url or '').strip()
        if not url:
            return HistoryStore.load()
        now = datetime.now().isoformat(timespec='seconds')
        entries = HistoryStore.load()
        for e in entries:
            if e.get('url') == url:
                e['last_used_at'] = now
                if title:
                    e['title'] = title
                if source:
                    e['source'] = source
                entries.remove(e)
                break
        entries.insert(0, {'url': url, 'title': title, 'source': source, 'parsed_at': now, 'last_used_at': now})
        entries = entries[:HistoryStore.MAX_ENTRIES]
        HistoryStore.save(entries)
        return entries

    @staticmethod
    def remove(url: str) -> List[Dict[str, Any]]:
        entries = [e for e in HistoryStore.load() if e.get('url') != url]
        HistoryStore.save(entries)
        return entries

    @staticmethod
    def clear() -> List[Dict[str, Any]]:
        HistoryStore.save([])
        return []


'''UiLogHandler'''


class UiLogHandler(logging.Handler):
    def __init__(self, sink):
        super(UiLogHandler, self).__init__(level=logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink(record.levelname.lower(), record.getMessage())
        except Exception:
            pass


'''VideoDlService'''


class VideoDlService():
    def __init__(self, version: str = '1.0.0'):
        self.version = version
        with diag.step('core', 'load config'):
            self.config = Config.load()
        self.bus = ProgressBus.instance()
        diag.log('core', f'config loaded: work_dir={self.config.work_dir!r} threadings={self.config.num_threadings} common_only={self.config.apply_common_clients_only}')
        # logs
        self._log_lock = threading.Lock()
        self._log_seq = 0
        self._logs: deque = deque(maxlen=800)
        # parsed videos (keep the original VideoInfo objects in memory)
        self._parsed: Dict[str, Any] = {}
        self._parsed_lock = threading.Lock()
        # jobs
        self._jobs_lock = threading.RLock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._queue: 'queue.Queue[str]' = queue.Queue()
        # parse history (persisted across launches)
        self.history: List[Dict[str, Any]] = HistoryStore.load()
        # engine (loaded lazily: only when the first parse is requested)
        self._imported = False
        self._import_error: Optional[str] = None
        self._engine_state = 'unloaded'  # unloaded | loading | ready | error
        self.engine_version = ''
        self._import_lock = threading.Lock()
        self._client = None
        self._client_signature: Optional[tuple] = None
        self._client_lock = threading.Lock()
        # the VideoClient built for the currently-running download job (set by
        # `_runjob`, cleared in its `finally`). `_sourceclient` reuses it so
        # the `allowed` restriction is honoured and we don't rebuild with the
        # full registered-modules list. Only accessed from the worker thread.
        self._active_client = None
        self.source_names: List[str] = []
        self.common_source_names: List[str] = []
        # worker
        self._worker = threading.Thread(target=self._workerloop, name='vd-download-worker', daemon=True)
        self._worker.start()
        self._poll_count = 0
        diag.log('core', 'download worker thread started')
        self.log('info', 'desktop backend is ready, waiting for the vd engine to be loaded')

    '''-------------------- logging --------------------'''

    def log(self, level: str, message: str) -> None:
        # the vd logger colorizes some messages, strip the ansi codes for the ui
        text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', str(message))
        with self._log_lock:
            self._log_seq += 1
            self._logs.append({'seq': self._log_seq, 'level': level, 'message': text, 'time': datetime.now().strftime('%H:%M:%S')})

    def logs(self, after_seq: int = 0) -> List[Dict[str, Any]]:
        with self._log_lock:
            return [item for item in self._logs if item['seq'] > after_seq]

    @property
    def log_seq(self) -> int:
        with self._log_lock:
            return self._log_seq

    '''-------------------- engine --------------------'''

    def ensureengine(self, wait: bool = False) -> str:
        '''
        Lazily load the vd engine. The app starts with the engine UNLOADED so the
        window shows up instantly; the first parse request triggers the load.
        Returns the current state: unloaded | loading | ready | error.
        With wait=True the calling thread performs the load (idempotent, thread-safe).
        '''
        if self._imported:
            return 'ready'
        if self._import_error:
            return 'error'
        if not wait:
            # kick off a background load without blocking the caller
            if self._engine_state == 'unloaded':
                threading.Thread(target=self.ensureengine, kwargs={'wait': True}, name='vd-engine-loader', daemon=True).start()
            return self._engine_state
        with self._import_lock:
            if self._imported:
                return 'ready'
            if self._import_error:
                return 'error'
            self._engine_state = 'loading'
            self.log('info', 'loading the vd engine (parsers will load on demand when you paste a url)')
            try:
                if str(VD_SRC) not in sys.path and VD_SRC.exists():
                    sys.path.insert(0, str(VD_SRC))
                with diag.step('engine', 'install progress hook'):
                    install_progress_hook()
                with diag.step('engine', 'import vd.modules (lazy mode)'):
                    from vd.modules import VideoClientBuilder, CommonVideoClientBuilder, BuildVideoClient, BuildCommonVideoClient
                with diag.step('engine', 'import vd.vd.VideoClient'):
                    from vd.vd import VideoClient
                with diag.step('engine', 'import engine version'):
                    from vd import __version__ as engine_version
                self.VideoClientBuilder = VideoClientBuilder
                self.CommonVideoClientBuilder = CommonVideoClientBuilder
                self.VideoClientCls = VideoClient
                self.BuildVideoClient = BuildVideoClient
                self.BuildCommonVideoClient = BuildCommonVideoClient
                # pull in the parser-name tables from the lazy __init__.py modules
                # so we can discover platform parsers without importing their code
                with diag.step('engine', 'load parser name tables'):
                    from vd.modules import sources as _sources_pkg
                    from vd.modules import common as _common_pkg
                    self._platform_parser_table: List[tuple] = list(getattr(_sources_pkg, '_EAGER_PARSERS', []))
                    self._common_parser_table: List[tuple] = list(getattr(_common_pkg, '_EAGER_COMMON', []))
                # Dynamic cleanup: filter stale config entries against the live
                # parser tables. This auto-adapts to any future parser removals.
                valid_parser_classes = {cls_name for _, cls_name in self._platform_parser_table + self._common_parser_table}
                cfg_changed = False
                if self.config.allowed_sources:
                    filtered = [s for s in self.config.allowed_sources if s in valid_parser_classes]
                    if len(filtered) != len(self.config.allowed_sources):
                        self.config.allowed_sources = filtered
                        cfg_changed = True
                if self.config.per_source_cookies:
                    filtered_cookies = {k: v for k, v in self.config.per_source_cookies.items() if k in valid_parser_classes}
                    if len(filtered_cookies) != len(self.config.per_source_cookies):
                        self.config.per_source_cookies = filtered_cookies
                        cfg_changed = True
                if cfg_changed:
                    self.config.save()
                    diag.log('core', 'cleaned stale parser entries from user config')
                self.source_names: List[str] = []
                self.common_source_names: List[str] = []
                self.engine_version = engine_version
                # forward the engine logs into the ui log panel
                with diag.step('engine', 'attach ui log handler'):
                    logging.getLogger('vd').addHandler(UiLogHandler(self.log))
                self.log('info', f'vd engine v{engine_version} ready (lazy mode: {len(self._platform_parser_table)} platform + {len(self._common_parser_table)} generic parsers available on demand)')
                diag.log('engine', f'engine ready v{engine_version} (lazy mode)')
                self._imported = True
                self._engine_state = 'ready'
            except Exception as err:
                self._import_error = f'{err}\n{traceback.format_exc()}'
                self._engine_state = 'error'
                diag.log('engine', f'engine load FAILED: {err}', 'error')
                self.log('error', f'failed to load the vd engine: {err}')
                self.log('debug', traceback.format_exc())
            return self._engine_state

    @property
    def engineready(self) -> bool:
        return self._imported

    @property
    def engine_state(self) -> str:
        return self._engine_state

    @property
    def engineerror(self) -> Optional[str]:
        return self._import_error

    def _humanize_error(self, after_seq: int) -> str:
        '''Turn the engine error logs captured during a download into a user-facing reason.'''
        errs = [l['message'] for l in self.logs(after_seq) if l['level'] == 'error']
        if not errs:
            return '下载失败：未获取到有效下载地址（链接可能已失效，或源站需要登录/代理）'
        last = errs[-1]
        m = last.lower()
        if '403' in last:
            return '下载失败：源站拒绝访问(403)，可能链接过期、需要登录 Cookie 或被风控；可在设置中填写 Cookie/代理后重试。'
        if '412' in last:
            return '下载失败：被源站风控拦截(412)，请稍后重试，或在设置中填写浏览器 Cookie。'
        if '404' in last:
            return '下载失败：资源不存在(404)，视频可能已被删除或链接无效。'
        if 'timed out' in m or 'timeout' in m:
            return '下载失败：网络超时，请检查网络或代理设置。'
        if 'ffmpeg' in m:
            return '下载失败：调用 ffmpeg 失败，请确认已安装 ffmpeg 并加入系统 PATH。'
        if 'no scheme supplied' in m or 'invalid url' in m:
            return '下载失败：下载地址无效，源站未返回可用的媒体地址。'
        if 'denied' in m or 'permission' in m:
            return '下载失败：写入被拒绝，请检查保存目录的写入权限。'
        return '下载失败：' + last[:200]

    def _buildrequests_overrides(self) -> Dict[str, Dict[str, Any]]:
        overrides: Dict[str, Any] = {}
        if self.config.proxy.strip():
            proxy = self.config.proxy.strip()
            if '://' not in proxy:
                proxy = 'http://' + proxy
            proxies = {'http': proxy, 'https': proxy}
        else:
            proxies = {}
        # global cookies as the fallback baseline for every source
        base_cookies = self._parsecookies(self.config.cookies)
        # per-source login cookies captured via the in-app login window
        per_source = dict(self.config.per_source_cookies or {})
        if not proxies and not base_cookies and not per_source:
            return {}
        # Use the live, currently-registered parser names instead of the cached
        # self.source_names / self.common_source_names (those are refreshed only at
        # the *end* of _buildclient, so a parser imported earlier in this same build
        # would otherwise be missing here and never receive its per-source login
        # cookies). Keeping the key set authoritative here guarantees every loaded
        # platform/generic parser gets its cookie entry.
        for name in list(self.VideoClientBuilder.REGISTERED_MODULES.keys()) + list(self.CommonVideoClientBuilder.REGISTERED_MODULES.keys()) + ['WebMediaGrabber']:
            per: Dict[str, Any] = {}
            if proxies:
                per['proxies'] = proxies
            # merge global + per-source cookies (per-source wins on conflict)
            merged = dict(base_cookies)
            src_cookies = self._parsecookies(per_source.get(name, ''))
            merged.update(src_cookies)
            if merged:
                per['cookies'] = merged
            overrides[name] = per
        return overrides

    @staticmethod
    def _parsecookies(cookie_string: str) -> Dict[str, str]:
        cookies: Dict[str, str] = {}
        for part in (cookie_string or '').split(';'):
            part = part.strip()
            if not part or '=' not in part:
                continue
            key, value = part.split('=', 1)
            cookies[key.strip()] = value.strip()
        return cookies

    def _buildclient(self, force: bool = False, allowed: Optional[List[str]] = None):
        '''Build (or reuse) the vd VideoClient according to the current config.
        `allowed` overrides the parser-name list used by the VideoClient. When the
        caller passes nothing, every parser that has been registered so far is
        used (i.e. only the ones the user has actually needed up to this point).
        '''
        self.ensureengine(wait=True)
        if not self.engineready:
            return None
        if allowed is None:
            allowed = list(self.VideoClientBuilder.REGISTERED_MODULES.keys())
            if not self.config.apply_common_clients_only:
                allowed = allowed + list(self.CommonVideoClientBuilder.REGISTERED_MODULES.keys())
        if self.config.allowed_sources:
            allowed = [s for s in allowed if s in set(self.config.allowed_sources)]
        # NOTE: per_source_cookies is a dict that login.py mutates in place. If we
        # put the *live dict reference* into the signature tuple, in-place updates
        # keep the same object id, so the signature never changes and the
        # VideoClient is never rebuilt — silently ignoring freshly captured login
        # cookies. Serialize the contents instead so any change is detected and
        # forces a rebuild with the new cookies.
        cookies_sig = tuple(sorted((str(k), str(v)) for k, v in (self.config.per_source_cookies or {}).items()))
        signature = (self.config.work_dir, self.config.num_threadings, self.config.proxy, self.config.cookies, cookies_sig, self.config.apply_common_clients_only, tuple(sorted(allowed)))
        with self._client_lock:
            if self._client is not None and self._client_signature == signature and not force:
                return self._client
            work_dir = self.config.work_dir or defaultworkdir()
            Path(work_dir).mkdir(parents=True, exist_ok=True)
            all_names = allowed + ['WebMediaGrabber']
            init_cfg = {name: {'work_dir': work_dir} for name in all_names}
            threadings = {name: max(1, int(self.config.num_threadings or 1)) for name in all_names}
            diag.log('core', f'building VideoClient (work_dir={work_dir!r}, threadings={self.config.num_threadings}, common_only={self.config.apply_common_clients_only}, parsers={len(allowed)})')
            with diag.step('core', 'construct vd VideoClient'):
                self._client = self.VideoClientCls(
                    allowed_video_sources=allowed,
                    init_video_clients_cfg=init_cfg,
                    clients_threadings=threadings,
                    requests_overrides=self._buildrequests_overrides(),
                    apply_common_video_clients_only=self.config.apply_common_clients_only,
                )
            self._client_signature = signature
            self.source_names = sorted(self.VideoClientBuilder.REGISTERED_MODULES.keys())
            self.common_source_names = sorted(self.CommonVideoClientBuilder.REGISTERED_MODULES.keys())
            diag.log('core', f'VideoClient ready ({len(self.source_names)} platform + {len(self.common_source_names)} generic parsers currently loaded)')
            return self._client

    def _ensure_parsers_for_url(self, url: str) -> List[str]:
        '''Heuristically import the parser modules that may handle `url`, so the
        vd engine only loads what the user actually needs.

        Rules ("用到几个，加载几个，不要全加载"):
          * if the URL hostname matches a known platform parser's module name,
            load ONLY that matched parser — no common parsers, no other
            platform parsers. The matched parser handles the URL with its
            original quality, and WebMediaGrabber (always present) acts as the
            universal fallback ("通用兜底"). Pre-loading common parsers would
            give `parsefromurl` a chance to fall back to a lower-quality
            generic parser (e.g. XiazaitoolVideoClient) even when the platform
            parser is loaded — that was the source of the "下载后质量差" bug.
          * if no platform parser matches the hostname (generic tools, short
            links, unknown sites), load only the common parsers so generic
            URLs still have a handler. We deliberately do NOT do a full
            platform import ("全加载" of all ~71 platform parsers) here — that
            defeats the whole point of lazy loading.

        Returns the class names that should go into `VideoClient`'s
        `allowed_video_sources`, so `parse()` can build a client containing
        exactly the matched parsers instead of every registered parser.
        '''
        self.ensureengine(wait=True)
        if not self.engineready:
            return []
        try:
            hostname = (urlsplit(url if '://' in url else f'https://{url}').hostname or '').lower()
        except Exception:
            hostname = ''
        matched_classes: List[str] = []
        matched_any = False
        for module_name, class_name in self._platform_parser_table:
            short_name = class_name.replace('VideoClient', '').lower()
            if module_name in hostname or short_name in hostname:
                matched_any = True
                if self._import_lazy_module('vd.modules.sources', module_name):
                    matched_classes.append(class_name)
        if matched_any:
            # Matched platform URL: load ONLY the matched platform parser(s).
            # No common parsers, no other platform parsers. The matched parser
            # handles the URL; WebMediaGrabber is the universal fallback.
            diag.log('core', f'matched platform parsers for {hostname!r}: {matched_classes} (no common pre-loaded)')
        else:
            # Unmatched URL (generic tools / short links / unknown sites):
            # do NOT do a full platform import. Load only the common parsers
            # so generic-tool URLs still have a handler.
            diag.log('core', f'no platform match for hostname {hostname!r}; loading common parsers only (no full platform import)')
            for module_name, class_name in self._common_parser_table:
                if self._import_lazy_module('vd.modules.common', module_name):
                    matched_classes.append(class_name)
        # refresh the cached source-name lists so the UI shows the current count
        self.source_names = sorted(self.VideoClientBuilder.REGISTERED_MODULES.keys())
        self.common_source_names = sorted(self.CommonVideoClientBuilder.REGISTERED_MODULES.keys())
        return matched_classes

    def _import_lazy_module(self, package: str, module_name: str) -> bool:
        '''Return True when the parser module is available (either just imported
        or loaded earlier in this process). Return False only when the import
        actually failed.

        Returning False for "already in sys.modules" used to silently drop the
        matching class on the second+ parse of the same platform: the first
        parse imports `vd.modules.sources.bilibili`, the second parse
        finds the module already loaded, the helper returned False, and
        `_ensure_parsers_for_url` skipped appending `BilibiliVideoClient` to
        `matched_classes` (it logged `matched []`). The parse then fell back
        to the full `REGISTERED_MODULES` path and the subsequent download
        produced a 0-byte file because the live `VideoClient` no longer
        matched the parser the `video_info` had been produced by.
        '''
        full_name = f'{package}.{module_name}'
        try:
            if full_name in sys.modules:
                return True
            importlib.import_module(full_name)
            return True
        except Exception as err:
            diag.log('core', f'  lazy-load {full_name} failed: {type(err).__name__}: {err}')
            return False

    def _sourceclient(self, source: str):
        '''Return the (lazily instantiated) per-platform client that owns this source.'''
        # reuse the job-scoped client if one is active (so we honour the
        # `allowed` restriction built by `_runjob`); otherwise fall back to
        # a full-rebuild client. Always accessed from the worker thread, so
        # the `_active_client` read is single-threaded by construction.
        client = self._active_client or self._buildclient()
        if client is None:
            return None
        if source == getattr(client.web_media_grabber, 'source', 'WebMediaGrabber'):
            return client.web_media_grabber
        for container, builder in ((client.video_clients, self.BuildVideoClient), (client.common_video_clients, self.BuildCommonVideoClient)):
            entry = container.get(source)
            if entry is None:
                continue
            if isinstance(entry, dict):
                entry = builder(module_cfg=entry['cfg'])
                container[source] = entry
            return entry
        return None

    def downloadvideoinfo(self, video_info) -> list:
        '''
        Download one VideoInfo through its own client.
        NOTE: `vd.vd.VideoClient.download` collects the results but never returns them
        (upstream bug), so we call the per-source client directly to get a reliable result.
        '''
        source = str(getattr(video_info, 'source', '') or '')
        target = self._sourceclient(source)
        if target is None:
            raise RuntimeError(f'no video client is available for source: {source}')
        client = self._client
        overrides = (getattr(client, 'requests_overrides', {}) or {}).get(source, {})
        return target.download([video_info], num_threadings=max(1, int(self.config.num_threadings or 1)), request_overrides=overrides) or []

    def prewarm(self) -> None:
        '''Lazily start the engine load in the background (no startup cost).'''
        self.ensureengine(wait=False)

    '''-------------------- parse --------------------'''

    def parse(self, url: str) -> Dict[str, Any]:
        url = (url or '').strip()
        if not url:
            return {'ok': False, 'error': 'please enter a video url', 'items': []}
        # the engine is loaded on demand here (first parse after launch)
        self.ensureengine(wait=True)
        if not self.engineready:
            return {'ok': False, 'error': f'the vd engine failed to load: {self._import_error}', 'items': []}
        # Normalize scheme-less inputs (e.g. "bilibili.com/video/BVxxx") to
        # `https://...` before handing the URL to vd. Without this, the
        # WebMediaGrabber's HEAD/GET probe (grabber.py) calls `raise_for_status()`
        # on the None returned by `BaseVideoClient.get` when `requests` raises
        # `InvalidURL`/`MissingSchema`; its except only catches
        # `requests.RequestException`, so the AttributeError leaks out as
        # "解析失败: 'NoneType' object has no attribute 'raise_for_status'".
        # The hostname already gets https-prefixed in `_ensure_parsers_for_url`
        # via `urlsplit(url if '://' in url else f'https://{url}')`; we just
        # extend the same convention to the URL itself so the vd parser
        # also sees a valid scheme.
        if '://' not in url:
            url = 'https://' + url
            self.log('info', f'normalized scheme-less url to: {url}')
        # heuristic parser load: only the platform parsers that match this URL
        # get imported, plus the small set of generic parsers as a fallback.
        matched = self._ensure_parsers_for_url(url)
        if matched:
            self.log('info', f'lazy-loaded parsers for this url: {matched}')
        # build the VideoClient with ONLY the parsers matched for this URL
        # (see `_ensure_parsers_for_url` for the "用到几个" rules). Passing
        # `allowed` keeps the running client minimal — it won't accumulate
        # every parser that has ever been touched across the session.
        client = self._buildclient(allowed=matched or None)
        self.config.last_url = url
        self.config.save()
        self.log('info', f'parsing url: {url}')
        try:
            with diag.step('core', f'parsefromurl({url[:80]})'):
                video_infos = client.parsefromurl(url=url) or []
        except Exception as err:
            self.log('error', f'parse failed: {err}')
            self.log('debug', traceback.format_exc())
            return {'ok': False, 'error': str(err), 'items': []}
        items = []
        for video_info in video_infos:
            items.append(self._tometaitem(video_info))
        if not items:
            self.log('warning', 'no playable media was found for this url')
        else:
            self.log('info', f'parsed {len(items)} media item(s)')
        # record the url into the persistent parse history
        try:
            first_title = items[0].get('title', '') if items else ''
            first_source = items[0].get('source', '') if items else ''
            self.history = HistoryStore.add(url, first_title, first_source)
        except Exception:
            pass
        return {'ok': True, 'error': '', 'items': items, 'work_dir': self.config.work_dir, 'history': list(self.history[:50])}

    def parse_batch(self, urls: List[str]) -> Dict[str, Any]:
        '''Parse several urls in one call (batch parsing). Reuses `parse()` for
        each url so per-url client building / history recording stays identical,
        then merges the resulting items into a single list. Item keys are uuids
        so they never collide across urls. Returns the same shape as `parse()`
        plus `batch=True`, `url_count` and a `errors` list for per-url failures.'''
        items: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        for url in urls or []:
            url = (url or '').strip()
            if not url:
                continue
            # Normalize scheme-less inputs the same way `parse()` does.
            if '://' not in url:
                url = 'https://' + url
            if not self.engineready:
                self.ensureengine(wait=True)
                if not self.engineready:
                    errors.append({'url': url, 'error': f'the vd engine failed to load: {self._import_error}'})
                    continue
            try:
                result = self.parse(url)
            except Exception as err:
                errors.append({'url': url, 'error': str(err)})
                continue
            if not result.get('ok') and not result.get('items'):
                errors.append({'url': url, 'error': result.get('error') or '解析失败'})
            for it in (result.get('items') or []):
                items.append(it)
        return {
            'ok': bool(items), 'error': '', 'items': items, 'errors': errors,
            'batch': True, 'url_count': len(urls or []),
            'work_dir': self.config.work_dir, 'history': list(self.history[:50]),
        }

    def _tometaitem(self, video_info) -> Dict[str, Any]:
        import uuid
        key = uuid.uuid4().hex
        with self._parsed_lock:
            self._parsed[key] = video_info
        title = ''
        save_path = ''
        ext = ''
        source = ''
        cover_url = ''
        try:
            title = str(video_info.title or '')
            save_path = str(video_info.save_path or '')
            ext = str(video_info.ext or '')
            source = str(video_info.source or '')
            cover_url = str(video_info.cover_url or '')
        except Exception:
            pass
        if not title and save_path:
            title = Path(save_path).name
        download_url = ''
        try:
            download_url = str(video_info.download_url or '')
        except Exception:
            pass
        # best-effort read of a per-item quality/definition label. vd's
        # VideoInfo has no first-class `quality` field, but parsers may stash it in
        # `_extra` (e.g. the bilibili parser now sets `quality` per qn); fall back
        # to '' when absent so the frontend simply hides the quality tag.
        quality = ''
        try:
            qv = video_info.get('quality') or video_info.get('definition') or video_info.get('qn_label')
            quality = str(qv) if qv else ''
        except Exception:
            quality = ''
        return {
            'key': key, 'title': title or 'untitled', 'source': source, 'ext': ext,
            'cover_url': cover_url, 'save_path': save_path, 'valid': bool(video_info.with_valid_download_url),
            'has_audio': bool(video_info.with_valid_audio_download_url), 'quality': quality,
            'download_url': download_url[:600], 'err_msg': str(getattr(video_info, 'err_msg', '') or ''),
        }

    '''-------------------- download --------------------'''

    def enqueue(self, keys: List[str], work_dir: Optional[str] = None) -> Dict[str, Any]:
        import uuid
        # NOTE: do NOT rebuild the VideoClient here. enqueue() runs on the
        # pywebview bridge thread (the thread that services JS calls), so a
        # synchronous `_buildclient(force=True)` blocks the bridge for several
        # seconds, freezes the "下载选中" button and makes it appear completely
        # unresponsive. The download worker thread builds its own job-scoped
        # client in `_runjob` and reads `self.config.*` live, so any
        # work_dir / proxy / cookies change saved by the GUI is honoured by
        # the worker without us pre-building on the bridge thread.
        if work_dir and work_dir.strip():
            new_wd = work_dir.strip()
            if new_wd != self.config.work_dir:
                self.config.work_dir = new_wd
                self.config.save()
        with self._parsed_lock:
            valid_keys = [k for k in keys if k in self._parsed]
        if not valid_keys:
            return {'ok': False, 'error': 'no valid media selected'}
        job_id = uuid.uuid4().hex[:12]
        items = []
        for key in valid_keys:
            with self._parsed_lock:
                video_info = self._parsed[key]
            items.append({
                'key': key, 'title': str(getattr(video_info, 'title', '') or Path(str(getattr(video_info, 'save_path', '') or '')).name or 'untitled'),
                'save_path': str(getattr(video_info, 'save_path', '') or ''), 'status': 'queued', 'error': '',
            })
        job = {
            'id': job_id, 'items': items, 'status': 'queued', 'error': '', 'created_at': datetime.now().strftime('%H:%M:%S'),
            'started_at': '', 'finished_at': '', 'work_dir': self.config.work_dir, 'cancel_event': threading.Event(),
            'done_count': 0, 'total_count': len(items), 'url': self.config.last_url,
        }
        with self._jobs_lock:
            self._jobs[job_id] = job
        self._queue.put(job_id)
        self.log('info', f'job {job_id} queued: {len(items)} item(s)')
        return {'ok': True, 'job_id': job_id}

    def cancel(self, job_id: str) -> Dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if not job:
            return {'ok': False, 'error': 'job not found'}
        job['cancel_event'].set()
        if job['status'] in {'queued', 'downloading'}:
            job['status'] = 'cancelling'
        self.log('warning', f'job {job_id} is being cancelled')
        return {'ok': True}

    def clearjobs(self) -> Dict[str, Any]:
        with self._jobs_lock:
            finished = [job_id for job_id, job in self._jobs.items() if job['status'] in {'done', 'error', 'cancelled'}]
            for job_id in finished:
                self._jobs.pop(job_id, None)
        return {'ok': True, 'removed': len(finished)}

    def _getjob(self, job_id: str):
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def _workerloop(self) -> None:
        diag.log('core', 'worker loop entered (waiting for jobs)')
        while True:
            job_id = self._queue.get()
            diag.log('core', f'worker picked up job {job_id}')
            job = self._getjob(job_id)
            if job is None:
                diag.log('core', f'job {job_id} disappeared before start', 'warning')
                continue
            self._runjob(job)
            self._queue.task_done()

    def _runjob(self, job: Dict[str, Any]) -> None:
        diag.log('core', f"job {job['id']} started ({job['total_count']} item(s))")
        job['status'] = 'downloading'
        job['started_at'] = datetime.now().strftime('%H:%M:%S')
        cancel_event: threading.Event = job['cancel_event']
        self.bus.reset()
        self.bus.set_interrupt(cancel_event.is_set)
        try:
            # build the download client with ONLY the parsers that produced
            # the items in this job. No unused parsers get instantiated, so
            # the client stays minimal even after many varied URLs.
            sources_needed: List[str] = []
            for _it in job['items']:
                with self._parsed_lock:
                    _vi = self._parsed.get(_it['key'])
                if _vi is not None:
                    _src = str(getattr(_vi, 'source', '') or '')
                    if _src and _src not in sources_needed and _src != 'WebMediaGrabber':
                        sources_needed.append(_src)
            client = self._buildclient(allowed=sources_needed or None)
            if client is None:
                raise RuntimeError(self._import_error or 'the vd engine is not available')
            # cache the job-scoped client so `_sourceclient` reuses it
            # instead of rebuilding with allowed=None (which would re-add
            # every previously-registered parser and defeat the purpose).
            self._active_client = client
            for item in job['items']:
                if cancel_event.is_set():
                    break
                with self._parsed_lock:
                    video_info = self._parsed.get(item['key'])
                if video_info is None:
                    item['status'] = 'error'
                    item['error'] = 'the media item has expired, please parse the url again'
                    continue
                item['status'] = 'downloading'
                self.log('info', f"[{job['id']}] downloading: {item['title']}")
                seq0 = self.log_seq
                try:
                    downloaded = self.downloadvideoinfo(video_info)
                except DownloadCancelled:
                    raise
                except Exception as err:
                    downloaded = []
                    self.log('error', f"[{job['id']}] download error: {err}")
                    self.log('debug', traceback.format_exc())
                if downloaded:
                    item['status'] = 'done'
                    first = downloaded[0]
                    try:
                        item['save_path'] = str(first.save_path or item['save_path'])
                    except Exception:
                        pass
                    job['done_count'] += 1
                    self.log('info', f"[{job['id']}] saved to: {item['save_path']}")
                else:
                    item['status'] = 'error'
                    item['error'] = self._humanize_error(seq0)
            if cancel_event.is_set():
                job['status'] = 'cancelled'
                self.log('warning', f"job {job['id']} cancelled")
            else:
                job['status'] = 'done'
                self.log('info', f"job {job['id']} finished: {job['done_count']}/{job['total_count']} succeeded")
        except DownloadCancelled:
            job['status'] = 'cancelled'
            self.log('warning', f"job {job['id']} cancelled")
        except Exception as err:
            job['status'] = 'error'
            job['error'] = str(err)
            self.log('error', f"job {job['id']} failed: {err}")
            self.log('debug', traceback.format_exc())
        finally:
            self._active_client = None
            self.bus.set_interrupt(None)
            job['finished_at'] = datetime.now().strftime('%H:%M:%S')
            for item in job['items']:
                if item['status'] == 'downloading':
                    item['status'] = 'cancelled'
            diag.log('core', f"job {job['id']} finished with status={job['status']} done={job['done_count']}/{job['total_count']}")

    '''-------------------- state --------------------'''

    def jobsnapshot(self) -> List[Dict[str, Any]]:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        result = []
        for job in jobs:
            items_meta = [{'title': i['title'], 'save_path': i['save_path'], 'status': i['status'], 'error': i['error']} for i in job['items']]
            result.append({
                'id': job['id'], 'status': job['status'], 'error': job['error'], 'work_dir': job['work_dir'],
                'created_at': job['created_at'], 'started_at': job['started_at'], 'finished_at': job['finished_at'],
                'done_count': job['done_count'], 'total_count': job['total_count'],
                'url': job.get('url', ''), 'items': items_meta,
            })
        result.reverse()
        return result

    def state(self, after_seq: int = 0) -> Dict[str, Any]:
        # the frontend polls this every ~0.7s; keep a heartbeat so a poll-storm or a
        # slowly growing payload can be spotted in startup.log
        self._poll_count += 1
        if self._poll_count % 100 == 0:
            diag.log('core', f'state heartbeat: polled {self._poll_count} times, active_jobs={len(self._jobs)}, parsed_items={len(self._parsed)}')
        started = time.perf_counter()
        result = {
            'jobs': self.jobsnapshot(), 'progress': self.bus.snapshot(),
            'logs': self.logs(after_seq), 'log_seq': self.log_seq,
            'engine_ready': self.engineready, 'engine_error': self.engineerror,
            'engine_state': self._engine_state, 'engine_version': self.engine_version,
            'history': list(self.history[:50]),
        }
        elapsed = (time.perf_counter() - started) * 1000
        if elapsed > 100:
            diag.log('core', f'state() took {elapsed:.0f}ms (payload may be growing)', 'warning')
        return result

    '''-------------------- misc helpers --------------------'''

    @staticmethod
    def openpath(path: str) -> Dict[str, Any]:
        try:
            target = Path(path)
            if target.is_file():
                os.startfile(str(target.parent))
            elif target.is_dir():
                os.startfile(str(target))
            else:
                return {'ok': False, 'error': f'path does not exist: {path}'}
            return {'ok': True}
        except Exception as err:
            return {'ok': False, 'error': str(err)}

    @staticmethod
    def openurl(url: str) -> Dict[str, Any]:
        try:
            import webbrowser
            webbrowser.open(url)
            return {'ok': True}
        except Exception as err:
            return {'ok': False, 'error': str(err)}
