'''
Function:
    Desktop backend core service
    - lazily imports the vd engine (it is heavy: 60+ platform parsers)
    - keeps the original `VideoInfo` objects alive so that no download capability is lost
    - runs downloads in a background thread pool and exposes plain dicts to the webview frontend
Author:
    CodeBuddy
'''
from __future__ import annotations

import os
import pickle
import re
import sys
import json
import shutil
import logging
import threading
import traceback
import subprocess
import time
import importlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor, Future, CancelledError
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlsplit

from . import diag
from .progress import ProgressBus, install_progress_hook, DownloadCancelled, DownloadPaused

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
#
# The same patch also tracks every child we spawn so we can forcefully kill them
# all on shutdown — otherwise ffmpeg/aria2c/node orphaned by a cancelled download
# (or a window that was closed mid-flight) keep running in the background after
# the app "exits". See `VideoDlService.shutdown()`.
_SUBPROCESS_REGISTRY: list = []  # live Popen objects spawned by this process


def _looks_like_real_video(save_path) -> bool:
    """Validate that a downloaded file actually looks like a real video, not the
    small error page that some CDNs (e.g. iesdouyin/aweme/v1/play under wind
    control) return with HTTP 200 and a forged ``video/mp4`` Content-Type.
    A real mp4/mov has an ``ftyp`` box at offset 4..7, and is never only a few
    kilobytes. Returns False if the file is missing, too small, or has no
    ``ftyp`` box — all strong signals the upstream returned an error body
    instead of the actual media stream."""
    if not save_path:
        return False
    try:
        p = Path(save_path)
        if not p.exists():
            return False
        # 16KB 远大于任何"200 + 错误页"体积(iesdouyin 风控页 < 8KB)，
        # 但远小于任何真实短视频的 ftyp+moov 体积(> 50KB)。
        if p.stat().st_size < 16384:
            return False
        with open(p, 'rb') as f:
            head = f.read(32)
        # mp4/mov/hevc/m4a 等 ISO BMFF box header: 4B size + 4B type，type 必为 'ftyp'。
        return len(head) >= 12 and head[4:8] == b'ftyp'
    except Exception:
        return False


def _kill_tracked_subprocesses() -> None:
    '''Terminate every child process we have spawned (ffmpeg/aria2c/node/...),
    RECURSIVELY killing its child tree.

    DrissionPage launches Chromium as a multi-process tree (browser + zygote +
    gpu + renderer). Killing only the top process left the renderer/gpu children
    orphaned — they kept eating hundreds of MB of RAM and lingered in the
    background after the window closed (the "5GB / leftover process" symptom).'''
    import psutil
    for proc in list(_SUBPROCESS_REGISTRY):
        try:
            if proc.poll() is None:
                try:
                    p = psutil.Process(proc.pid)
                    for child in p.children(recursive=True):
                        try:
                            child.kill()
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
        except Exception:
            pass
    _SUBPROCESS_REGISTRY.clear()


if os.name == 'nt':
    import subprocess as _subprocess
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen_init = _subprocess.Popen.__init__

    def _no_window_popen_init(self, *args, **kwargs):
        kwargs.setdefault('creationflags', _CREATE_NO_WINDOW)
        _orig_popen_init(self, *args, **kwargs)
        # Keep only still-running children. Every Popen also holds an OS handle,
        # so an append-only registry would grow without bound over a long session
        # (and leak handles for long-finished ffmpeg/aria2c/node runs).
        _SUBPROCESS_REGISTRY[:] = [p for p in _SUBPROCESS_REGISTRY if p.poll() is None]
        _SUBPROCESS_REGISTRY.append(self)

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
# Only the three VERIFIED platform parsers ship enabled by default
# (douyin / bilibili / youtube — see docs/视频解析器构建流程.md). All other
# upstream platform parsers have been removed from the engine entirely.
DEFAULT_ALLOWED_SOURCES = ['DouyinVideoClient', 'BilibiliVideoClient', 'YouTubeVideoClient']


@dataclass
class Config():
    work_dir: str = field(default_factory=defaultworkdir)
    num_threadings: int = 5
    concurrent_downloads: int = 1
    proxy: str = ''
    cookies: str = ''
    # per-source login cookies captured via the in-app login window (DrissionPage).
    # keyed by the vd source class name, e.g. 'BilibiliVideoClient'.
    per_source_cookies: Dict[str, str] = field(default_factory=dict)
    # global preferred quality used for the "default selected quality" behaviour:
    # 'best' | '4k' | '1080p' | '720p' | '480p' | '360p' | 'auto'
    default_quality: str = 'best'
    apply_common_clients_only: bool = False
    # Download subtitles (when available) and mux them into the final video.
    download_subtitles: bool = True
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

    @staticmethod
    def jobspath() -> Path:
        '''Path to the persisted job store (lives next to config.json).'''
        return Config.configpath().parent / 'jobs.json'

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
            # Backwards compatibility: old configs used num_threadings for concurrency.
            if 'concurrent_downloads' not in known:
                known['concurrent_downloads'] = max(1, int(known.get('num_threadings', 2) or 2))
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

    def emit(self, record: 'logging.LogRecord') -> None:
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
        diag.log('core', f'config loaded: work_dir={self.config.work_dir!r} concurrent={self.config.concurrent_downloads} threadings={self.config.num_threadings}')
        # logs
        self._log_lock = threading.Lock()
        self._log_seq = 0
        self._logs: deque = deque(maxlen=800)
        # parsed videos (keep the original VideoInfo objects in memory)
        self._parsed: Dict[str, Any] = {}
        # key -> source url that produced the VideoInfo (needed to re-parse on
        # resume-after-restart, since VideoInfo objects cannot be serialized).
        self._parsed_url: Dict[str, str] = {}
        self._parsed_lock = threading.Lock()
        # jobs
        self._jobs_lock = threading.RLock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        # concurrent download executor: one thread per active item, up to
        # concurrent_downloads items at the same time.
        self._executor: Optional[ThreadPoolExecutor] = None
        self._executor_lock = threading.Lock()
        self._futures: Dict[Future, tuple] = {}
        self._futures_lock = threading.Lock()
        self._inflight: Set[tuple] = set()
        self._inflight_lock = threading.Lock()
        self._job_clients: Dict[str, Any] = {}
        self._job_clients_lock = threading.Lock()
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
        self.source_names: List[str] = []
        self.common_source_names: List[str] = []
        self._poll_count = 0
        diag.log('core', 'VideoDlService initialized')
        self.log('info', 'desktop backend is ready, waiting for the vd engine to be loaded')
        # Restore previously persisted jobs. Unfinished jobs come back as PAUSED
        # (shown but not started); the user starts them via the per-job
        # "开始/继续" button, which re-parses and resumes. See `_load_jobs`.
        self._load_jobs()
        # Restore the parsed VideoInfo cache OFF the startup critical path:
        # unpickling it pulls in engine modules (~0.4s of imports) and the
        # follow-up prune-and-save adds another ~0.2s. The cache is only needed
        # when the user resumes/retries a paused job, so load it in a daemon
        # thread shortly after boot — until then a resume just re-parses (the
        # lazy engine load costs more than that anyway).
        threading.Thread(target=self._restoreparsedcachebg, name='parsed-cache-loader', daemon=True).start()

    def _restoreparsedcachebg(self) -> None:
        '''Background twin of the old synchronous load+prune parsed-cache boot
        step (see __init__). Sleeps a moment first so the UI page load never
        competes with it for the GIL.'''
        try:
            time.sleep(1.0)
            self._load_parsed_cache()
            self._save_parsed_cache()
        except Exception as err:
            diag.log('core', f'background parsed cache restore failed: {err}', 'warning')

    @property
    def _effective_concurrent(self) -> int:
        return max(1, int(self.config.concurrent_downloads or self.config.num_threadings or 2))

    def _ensure_executor(self) -> None:
        '''Recreate the thread pool whenever the concurrency setting changes.'''
        wanted = self._effective_concurrent
        with self._executor_lock:
            if self._executor is not None and self._executor._max_workers == wanted:
                return
            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=False)
                except Exception:
                    pass
            self._executor = ThreadPoolExecutor(max_workers=wanted, thread_name_prefix='vd-item')
            diag.log('core', f'download executor resized to {wanted} worker(s)')

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
        # 去掉我们自己写的日志前缀（如 "[jobid] download error: "），避免内部日志直接暴露给用户
        errs = [re.sub(r'^\[[^\]]+\]\s*(?:download error:\s*)?', '', e) for e in errs]
        if not errs:
            return '下载失败：未获取到有效下载地址（链接可能已失效，或源站需要登录/代理）'
        last = errs[-1]
        m = last.lower()
        if 'list index out of range' in m or 'index out of range' in m:
            return '下载失败：未能解析到可用的视频流地址，请重新解析链接后重试。'
        if '403' in last:
            return '下载失败：源站拒绝访问(403)，可能链接过期、需要登录或被风控；请点击顶栏「登录态」按钮登录后重试。'
        if '412' in last:
            return '下载失败：被源站风控拦截(412)，请稍后重试，或点击顶栏「登录态」按钮登录该平台。'
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

    def _sourceclient(self, source: str, client=None):
        '''Return the (lazily instantiated) per-platform client that owns this source.
        If a job-scoped client is provided, use it; otherwise fall back to the
        global cached client.'''
        target_client = client or self._buildclient()
        if target_client is None:
            return None
        if source == getattr(target_client.web_media_grabber, 'source', 'WebMediaGrabber'):
            return target_client.web_media_grabber
        for container, builder in ((target_client.video_clients, self.BuildVideoClient), (target_client.common_video_clients, self.BuildCommonVideoClient)):
            entry = container.get(source)
            if entry is None:
                continue
            if isinstance(entry, dict):
                entry = builder(module_cfg=entry['cfg'])
                container[source] = entry
            return entry
        return None

    def downloadvideoinfo(self, video_info, client=None) -> list:
        '''
        Download one VideoInfo through its own client.
        NOTE: `vd.vd.VideoClient.download` collects the results but never returns them
        (upstream bug), so we call the per-source client directly to get a reliable result.
        '''
        source = str(getattr(video_info, 'source', '') or '')
        target = self._sourceclient(source, client)
        if target is None:
            raise RuntimeError(f'no video client is available for source: {source}')
        target_client = client or self._client
        overrides = dict((getattr(target_client, 'requests_overrides', {}) or {}).get(source, {}))
        # Propagate the user's "download subtitles" preference to the engine.
        # The engine pops this key before it reaches the network layer.
        overrides['download_subtitles'] = bool(self.config.download_subtitles)
        return target.download([video_info], num_threadings=1, request_overrides=overrides) or []

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
            # remember which url produced this item so a post-restart resume can
            # re-parse it (VideoInfo objects are not serializable).
            self._parsed_url[key] = self.config.last_url
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

    def _subtask_count_for(self, video_info) -> int:
        '''How many trackable sub-tasks a VideoInfo represents.

        A typical YouTube item has separate video + audio streams plus optional
        subtitle tracks. Counting these instead of whole items lets the UI say
        "2/3 done, 1 remaining" while an item is still in progress.'''
        count = 0
        try:
            if video_info.with_valid_download_url:
                count += 1
            if video_info.with_valid_audio_download_url:
                count += 1
            count += len(getattr(video_info, 'subtitles', None) or [])
        except Exception:
            pass
        return max(count, 1)

    def _identifier_for(self, video_info) -> str:
        '''Best-effort stable identifier for matching a restored item to a
        freshly parsed one. YouTube sets ``identifier`` to ``vid-label``; for
        other sources we fall back to the raw ``identifier`` field.'''
        try:
            identifier = str(getattr(video_info, 'identifier', '') or '')
            if identifier:
                return identifier
        except Exception:
            pass
        return ''

    '''-------------------- download --------------------'''

    def enqueue(self, keys: List[str], work_dir: Optional[str] = None) -> Dict[str, Any]:
        import uuid
        # NOTE: do NOT rebuild the VideoClient here. enqueue() runs on the
        # pywebview bridge thread (the thread that services JS calls), so a
        # synchronous `_buildclient(force=True)` blocks the bridge for several
        # seconds, freezes the "下载选中" button and makes it appear completely
        # unresponsive. The download worker thread builds its own job-scoped
        # client in `_process_item` and reads `self.config.*` live, so any
        # work_dir / proxy / cookies change saved by the GUI is honoured.
        if work_dir and work_dir.strip():
            new_wd = work_dir.strip()
            if new_wd != self.config.work_dir:
                self.config.work_dir = new_wd
                self.config.save()
        with self._parsed_lock:
            valid_keys = [k for k in keys if k in self._parsed]
        if not valid_keys:
            return {'ok': False, 'error': 'no valid media selected'}
        self._ensure_executor()
        job_ids: List[str] = []
        for key in valid_keys:
            job_id = uuid.uuid4().hex[:12]
            with self._parsed_lock:
                video_info = self._parsed[key]
                url_for_item = self._parsed_url.get(key)
            source = str(getattr(video_info, 'source', '') or '')
            sources_needed: List[str] = []
            if source and source != 'WebMediaGrabber':
                sources_needed.append(source)
            sub_count = self._subtask_count_for(video_info)
            identifier = self._identifier_for(video_info)
            item = {
                'key': key, 'title': str(getattr(video_info, 'title', '') or Path(str(getattr(video_info, 'save_path', '') or '')).name or 'untitled'),
                'save_path': str(getattr(video_info, 'save_path', '') or ''), 'status': 'queued', 'error': '',
                'subtask_count': sub_count, 'identifier': identifier,
            }
            job = {
                'id': job_id, 'items': [item], 'status': 'queued', 'error': '', 'created_at': datetime.now().strftime('%H:%M:%S'),
                'started_at': '', 'finished_at': '', 'work_dir': self.config.work_dir,
                'cancel_event': threading.Event(), 'pause_event': threading.Event(),
                'done_count': 0, 'total_count': sub_count, 'url': self.config.last_url,
                'urls': [url_for_item] if url_for_item else [], 'sources_needed': sources_needed, 'restored': False,
            }
            with self._jobs_lock:
                self._jobs[job_id] = job
            job_ids.append(job_id)
            future = self._executor.submit(self._process_item, job_id, key)
            with self._futures_lock:
                self._futures[future] = (job_id, key)
            future.add_done_callback(self._on_item_done)
        self._save_jobs()
        self._save_parsed_cache()
        self.log('info', f'enqueued {len(job_ids)} job(s): {job_ids}, concurrency={self._effective_concurrent}')
        return {'ok': True, 'job_id': job_ids[0], 'job_ids': job_ids, 'count': len(job_ids)}

    def _getjob(self, job_id: str):
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def _get_or_build_job_client(self, job_id: str, source: str):
        '''Return a job-scoped VideoClient, building it once per job on first use.'''
        with self._job_clients_lock:
            client = self._job_clients.get(job_id)
            if client is not None:
                return client
        job = self._getjob(job_id)
        # Build outside the lock to avoid holding it during the heavy import.
        # Prefer the pre-computed list of all sources needed by this job so a
        # multi-source job shares one client instead of rebuilding per item.
        sources_needed = (job.get('sources_needed') if job else None) or ([source] if source and source != 'WebMediaGrabber' else None)
        client = self._buildclient(allowed=sources_needed or None)
        if client is None:
            return None
        with self._job_clients_lock:
            # Another thread may have built it while we were outside the lock.
            self._job_clients.setdefault(job_id, client)
            return self._job_clients[job_id]

    def _process_item(self, job_id: str, item_key: str) -> None:
        '''Download a single item. Runs inside the thread pool.'''
        flight_key = (job_id, item_key)
        with self._inflight_lock:
            if flight_key in self._inflight:
                return  # another thread is already handling this item
            self._inflight.add(flight_key)

        try:
            self._do_process_item(job_id, item_key)
        except (DownloadPaused, DownloadCancelled):
            # Make sure any rich progress tasks left behind by the aborted
            # download are marked finished, otherwise the UI would sum them
            # with the fresh tasks created after resume/cancel.
            self.bus.finish_tasks_for(job_id, item_key)
            return
        finally:
            with self._inflight_lock:
                self._inflight.discard(flight_key)

    def _do_process_item(self, job_id: str, item_key: str) -> None:
        '''Core logic for downloading a single item.'''
        job = self._getjob(job_id)
        if job is None:
            return
        item = next((it for it in job['items'] if it['key'] == item_key), None)
        if item is None:
            return

        # Honour job-level cancellation before doing anything.
        if job['cancel_event'].is_set():
            item['status'] = 'cancelled'
            self._update_job_status(job)
            return

        # If the job is paused, block until resumed or cancelled.
        while job['pause_event'].is_set() and not job['cancel_event'].is_set():
            if item['status'] not in ('done', 'error', 'cancelled'):
                item['status'] = 'paused'
            self._update_job_status(job)
            time.sleep(0.2)

        if job['cancel_event'].is_set():
            item['status'] = 'cancelled'
            self._update_job_status(job)
            return

        if item['status'] not in ('queued', 'paused'):
            # Already handled by a previous run (e.g. resumed but item finished).
            return

        if not job['started_at']:
            job['started_at'] = datetime.now().strftime('%H:%M:%S')
        item['status'] = 'downloading'
        self._update_job_status(job)

        with self._parsed_lock:
            video_info = self._parsed.get(item_key)
        if video_info is None:
            item['status'] = 'error'
            item['error'] = 'the media item has expired, please parse the url again'
            self._update_job_status(job)
            return

        source = str(getattr(video_info, 'source', '') or '')
        client = self._get_or_build_job_client(job_id, source)
        if client is None:
            item['status'] = 'error'
            item['error'] = self._import_error or 'the vd engine is not available'
            self._update_job_status(job)
            return

        # Tag progress tasks created by the engine with this job/item so the UI
        # can place a progress bar under the right item.
        self.bus.set_context(job_id, item_key)
        self.bus.add_interrupt(job_id, job['cancel_event'].is_set)
        self.bus.add_pause(job_id, job['pause_event'].is_set)
        try:
            self.log('info', f"[{job['id']}] downloading: {item['title']}")
            seq0 = self.log_seq
            try:
                downloaded = self.downloadvideoinfo(video_info, client)
            except DownloadCancelled:
                item['status'] = 'cancelled'
                self.log('warning', f"[{job['id']}] cancelled item: {item['title']}")
                raise
            except DownloadPaused:
                item['status'] = 'paused'
                self.log('warning', f"[{job['id']}] paused: {item['title']}")
                self._update_job_status(job)
                self.bus.finish_tasks_for(job_id, item_key)
                return
            except Exception as err:
                downloaded = []
                # 引擎内部会捕获所有异常再以普通错误上抛，DownloadPaused/
                # DownloadCancelled 到这里已变了味。按事件状态兜底归类，
                # 避免"点暂停 → 任务直接失败"。
                if job['pause_event'].is_set():
                    item['status'] = 'paused'
                    self.log('warning', f"[{job['id']}] paused: {item['title']}")
                    self._update_job_status(job)
                    self.bus.finish_tasks_for(job_id, item_key)
                    return
                if job['cancel_event'].is_set():
                    item['status'] = 'cancelled'
                    self.log('warning', f"[{job['id']}] cancelled item: {item['title']}")
                    self._update_job_status(job)
                    self.bus.finish_tasks_for(job_id, item_key)
                    return
                self.log('error', f"[{job['id']}] download error: {err}")
                self.log('debug', traceback.format_exc())
            # downloaded 列表只表示"下载器认为成功"(HTTP 200)，不能信。
            # iesdouyin/aweme/v1/play 等接口在风控/缺签名时会返回 200 + 几 KB
            # 错误内容(Content-Type 伪装 video/mp4)，下载器无法识别。落盘前
            # 做最小体积 + mp4 magic bytes 校验，把"假成功"挡在外面。
            if downloaded:
                _valid = [d for d in downloaded if _looks_like_real_video(getattr(d, 'save_path', None))]
                if _valid:
                    downloaded = _valid
                else:
                    self.log('error', f"[{job['id']}] download returned but file is missing/invalid (wind-control?): {item['title']}")
                    item['status'] = 'error'
                    item['error'] = '下载完成但文件无效，请尝试登录抖音后重试，或检查视频是否已删除'
                    self._update_job_status(job)
                    self.bus.finish_tasks_for(job_id, item_key)
                    downloaded = []
            if downloaded:
                item['status'] = 'done'
                first = downloaded[0]
                try:
                    item['save_path'] = str(first.save_path or item['save_path'])
                except Exception:
                    pass
                with self._jobs_lock:
                    job['done_count'] += item.get('subtask_count', 1)
                self.log('info', f"[{job['id']}] saved to: {item['save_path']}")
            else:
                # 引擎也可能把暂停/取消异常吞掉后直接返回空列表（不抛异常）
                if job['pause_event'].is_set():
                    item['status'] = 'paused'
                    self.log('warning', f"[{job['id']}] paused: {item['title']}")
                    self._update_job_status(job)
                    return
                if job['cancel_event'].is_set():
                    item['status'] = 'cancelled'
                    self.log('warning', f"[{job['id']}] cancelled item: {item['title']}")
                    self._update_job_status(job)
                    return
                item['status'] = 'error'
                item['error'] = self._humanize_error(seq0)
        except DownloadCancelled:
            item['status'] = 'cancelled'
            self.log('warning', f"[{job['id']}] cancelled item: {item['title']}")
        finally:
            self.bus.clear_context()
            self.bus.remove_interrupt(job_id)
            self.bus.remove_pause(job_id)
            self._update_job_status(job)
            self._save_parsed_cache()
            self._save_jobs()

    def _update_job_status(self, job: Dict[str, Any]) -> None:
        '''Derive the job status from its item statuses.'''
        statuses = [it['status'] for it in job['items']]
        if job['cancel_event'].is_set() and all(s in ('cancelled', 'done', 'error') for s in statuses):
            job['status'] = 'cancelled'
        elif all(s == 'done' for s in statuses):
            job['status'] = 'done'
            job['finished_at'] = job['finished_at'] or datetime.now().strftime('%H:%M:%S')
            self.log('info', f"job {job['id']} finished: {job['done_count']}/{job['total_count']} succeeded")
        elif all(s == 'error' for s in statuses):
            job['status'] = 'error'
            job['finished_at'] = job['finished_at'] or datetime.now().strftime('%H:%M:%S')
        elif all(s == 'cancelled' for s in statuses):
            job['status'] = 'cancelled'
            job['finished_at'] = job['finished_at'] or datetime.now().strftime('%H:%M:%S')
        elif job['cancel_event'].is_set():
            # Preserve the explicit cancelling state while active items finish up.
            job['status'] = 'cancelling'
        elif any(s == 'downloading' for s in statuses):
            job['status'] = 'downloading'
        elif any(s == 'paused' for s in statuses):
            job['status'] = 'paused'
        else:
            job['status'] = 'queued'

    def _on_item_done(self, future: Future) -> None:
        '''Clean up the futures registry when an item task finishes.'''
        with self._futures_lock:
            self._futures.pop(future, None)
        try:
            future.result()
        except CancelledError:
            return  # cancelled before the task started running
        except (DownloadPaused, DownloadCancelled):
            return  # handled by _do_process_item; do not log as crash
        except Exception as err:
            diag.log('core', f'item task raised unhandled exception: {err}', 'error')

    def pause(self, job_id: str) -> Dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if not job:
            return {'ok': False, 'error': 'job not found'}
        if job['status'] in {'queued', 'downloading'}:
            job['status'] = 'pausing'
            job['pause_event'].set()
            self.log('warning', f'job {job_id} is being paused')
        self._save_jobs()
        return {'ok': True}

    def resume(self, job_id: str) -> Dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if not job:
            return {'ok': False, 'error': 'job not found'}
        # Ignore duplicate clicks while a reparse is already in flight.
        if job['status'] == 'resuming':
            return {'ok': True, 'async': True}
        if job['status'] not in {'paused', 'pausing', 'error'}:
            return {'ok': True}
        # Retry: requeue any errored items so they get downloaded again.
        if job['status'] == 'error':
            for item in job['items']:
                if item['status'] == 'error':
                    item['status'] = 'queued'
                    item['error'] = ''
        # After a restart the parsed VideoInfo objects are gone, so we can't just
        # re-submit the old keys. Re-parse the source urls to rebuild them and
        # remap unfinished items to fresh keys. Already-finished files on disk are
        # skipped; the rest re-download and the engine's own downloader resumes
        # from any existing .part file (this is the "断点续传" path).
        missing_in_memory = any(item['key'] not in self._parsed for item in job['items'])
        need_reparse = job.get('restored', False) or missing_in_memory
        self.log('info', f'job {job_id} resume requested: restored={job.get("restored", False)} missing_parsed={missing_in_memory}')
        if not need_reparse:
            # Fast path: nothing to re-parse, just wake up the workers.
            job['pause_event'].clear()
            job['status'] = 'downloading'
            self._submit_resumed_items(job)
            self._save_jobs()
            self.log('info', f'job {job_id} resumed')
            return {'ok': True}
        # Slow path: re-parsing can take several seconds (engine IO + possible
        # browser fallback). Run it on a background thread so the pywebview bridge
        # does not freeze the "恢复中" UI.
        job['status'] = 'resuming'
        job['pause_event'].clear()
        self._save_jobs()

        def _resume_worker():
            try:
                self._reparse_for_resume(job)
            except Exception as err:
                self.log('error', f'job {job_id} reparse failed: {err}')
                self.log('debug', traceback.format_exc())
            with self._jobs_lock:
                live_job = self._jobs.get(job_id)
            if live_job is not job:
                return
            # If the user cancelled while we were reparsing, do not restart downloads.
            if job.get('cancel_event') and job['cancel_event'].is_set():
                return
            # Something else changed the job state; respect it.
            if job.get('status') != 'resuming':
                return
            self._update_job_status(job)
            if job['status'] == 'error':
                self.log('warning', f'job {job_id} resume failed: reparse produced no usable items')
                self._save_jobs()
                return
            job['status'] = 'downloading'
            self._submit_resumed_items(job)
            self.log('info', f'job {job_id} resumed after reparse')

        threading.Thread(target=_resume_worker, name=f'resume-{job_id}', daemon=True).start()
        return {'ok': True, 'async': True}

    def _submit_resumed_items(self, job: Dict[str, Any]) -> None:
        '''Re-submit paused/queued items after a resume.'''
        job_id = job['id']
        self._ensure_executor()
        for item in job['items']:
            if item['status'] in ('paused', 'queued'):
                # Clear stale progress tasks from the previous run so the UI does
                # not sum old bytes with the new download.
                self.bus.finish_tasks_for(job_id, item['key'])
                flight_key = (job_id, item['key'])
                with self._inflight_lock:
                    already_running = flight_key in self._inflight
                if already_running:
                    item['status'] = 'downloading'
                    continue
                item['status'] = 'queued'
                future = self._executor.submit(self._process_item, job_id, item['key'])
                with self._futures_lock:
                    self._futures[future] = (job_id, item['key'])
                future.add_done_callback(self._on_item_done)
        self._save_jobs()

    def _reparse_for_resume(self, job: Dict[str, Any]) -> None:
        '''Re-parse the job's source urls to rebuild the (non-serializable)
        VideoInfo objects needed for downloading, and remap unfinished items to
        fresh parsed keys. Finished items whose file still exists are kept as
        done so they are not re-downloaded.'''
        import uuid
        self.ensureengine(wait=True)
        if not self.engineready:
            with self._jobs_lock:
                for item in job['items']:
                    if item['status'] not in ('done',):
                        item['status'] = 'error'
                        item['error'] = '引擎未就绪，无法继续，请回到主页重新解析链接'
            return
        urls = job.get('urls') or ([job['url']] if job.get('url') else [])
        sources_needed = job.get('sources_needed') or []
        # For jobs that we know belong to a specific platform parser, skip the
        # WebMediaGrabber fallback during reparse. The fallback can get stuck
        # probing direct media URLs that have expired (e.g. googlevideo 403)
        # and adds no value when the original parser is available.
        skip_web_fallback = bool(sources_needed) and all(s != 'WebMediaGrabber' for s in sources_needed)
        fresh_by_title: Dict[str, List[str]] = {}
        fresh_by_identifier: Dict[str, List[str]] = {}
        parsed_count = 0
        for url in urls:
            try:
                # Lazy-import the platform parser(s) for this url so the
                # job-scoped client actually contains the required source.
                self._ensure_parsers_for_url(url)
                client = self._buildclient(allowed=sources_needed or None)
                video_infos = client.parsefromurl(url=url, skip_web_media_grabber_fallback=skip_web_fallback) or []
            except Exception as err:
                self.log('error', f'reparse failed for {url}: {err}')
                continue
            with self._parsed_lock:
                for vi in video_infos:
                    k = uuid.uuid4().hex
                    self._parsed[k] = vi
                    self._parsed_url[k] = url
                    title = str(getattr(vi, 'title', '') or '').strip()
                    identifier = self._identifier_for(vi)
                    if identifier:
                        fresh_by_identifier.setdefault(identifier, []).append(k)
                    if title:
                        fresh_by_title.setdefault(title, []).append(k)
                    parsed_count += 1
        self.log('info', f'reparse for job {job["id"]}: parsed {parsed_count} item(s) from {len(urls)} url(s)')
        remapped = 0
        missing = 0
        with self._jobs_lock:
            for item in job['items']:
                if item['status'] == 'done' and item.get('save_path') and Path(item['save_path']).exists():
                    continue  # already downloaded, keep as done
                title = str(item.get('title', '') or '').strip()
                identifier = str(item.get('identifier', '') or '').strip()
                fresh: List[str] = []
                if identifier and identifier in fresh_by_identifier:
                    fresh = fresh_by_identifier[identifier]
                elif title and title in fresh_by_title:
                    fresh = fresh_by_title[title]
                if fresh:
                    new_key = fresh.pop(0)
                    new_vi = self._parsed.get(new_key)
                    item['key'] = new_key
                    item['status'] = 'queued'
                    item['error'] = ''
                    if new_vi is not None:
                        item['subtask_count'] = self._subtask_count_for(new_vi)
                        item['identifier'] = self._identifier_for(new_vi)
                    remapped += 1
                elif item['status'] != 'done':
                    item['status'] = 'error'
                    item['error'] = '重新解析后未找到该条目，请回到主页重新解析链接'
                    missing += 1
            job['total_count'] = sum(it.get('subtask_count', 1) for it in job['items'])
            job['done_count'] = sum(it.get('subtask_count', 1) for it in job['items'] if it['status'] == 'done')
            job['restored'] = False
        self.log('info', f'reparse for job {job["id"]}: remapped {remapped} item(s), {missing} missing; total_subtasks={job["total_count"]} done_subtasks={job["done_count"]}')
        self._save_parsed_cache()

    def cancel(self, job_id: str) -> Dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if not job:
            return {'ok': False, 'error': 'job not found'}
        # Terminal jobs are removed immediately so the X button always works.
        if job['status'] in {'done', 'error', 'cancelled'}:
            self._remove_job(job_id)
            return {'ok': True}
        job['cancel_event'].set()
        job['pause_event'].clear()
        # Mark all progress tasks for this job as finished immediately; active
        # downloads may create fresh updates for a moment, but stale tasks from
        # earlier attempts must not stack with new ones.
        self.bus.finish_tasks_for(job_id)
        if job['status'] in {'queued', 'downloading', 'paused', 'pausing', 'resuming'}:
            job['status'] = 'cancelling'
        # Cancel queued futures that have not started yet so they don't block.
        with self._futures_lock:
            for future, (jid, ikey) in list(self._futures.items()):
                if jid != job_id:
                    continue
                if future.cancel():
                    for item in job['items']:
                        if item['key'] == ikey:
                            item['status'] = 'cancelled'
                            break
        # Cancel any item that is not actively downloading right now. Active
        # downloads will finish cancellation themselves via the cancel_event.
        for item in job['items']:
            if item['status'] in ('queued', 'paused', 'error'):
                item['status'] = 'cancelled'
            if item['status'] != 'done':
                try:
                    part = Path(str(item['save_path'] or ''))
                    if part and part.suffix:
                        part_part = part.with_suffix(part.suffix + '.part')
                        if part_part.exists():
                            part_part.unlink()
                except Exception:
                    pass
        # If there is nothing left to cancel, mark the job as cancelled now.
        if all(it['status'] in ('done', 'cancelled', 'error') for it in job['items']):
            job['status'] = 'cancelled'
        self.log('warning', f'job {job_id} is being cancelled')
        self._save_jobs()
        # If everything was already terminal, remove the job from the UI now.
        if job['status'] in {'done', 'error', 'cancelled'}:
            self._remove_job(job_id)
        return {'ok': True}

    def clearjobs(self) -> Dict[str, Any]:
        removed = []
        with self._jobs_lock:
            finished = [job_id for job_id, job in self._jobs.items() if job['status'] in {'done', 'error', 'cancelled'}]
            for job_id in finished:
                removed.append(self._jobs.pop(job_id, None))
                with self._job_clients_lock:
                    self._job_clients.pop(job_id, None)
        for job in removed:
            self._cleanup_parsed_cache(job)
        self._save_jobs()
        self._save_parsed_cache()
        return {'ok': True, 'removed': len(finished)}

    '''-------------------- retry audio (补音频 / 重新合并) --------------------'''

    def retry_audio(self, job_id: str) -> Dict[str, Any]:
        '''Re-download just the audio for a finished item and merge it with the
        already-downloaded video file. This avoids re-parsing the page (which
        YouTube may rate-limit) by reusing the cached VideoInfo plus a fresh
        yt-dlp-resolved (n-decrypted) audio url.'''
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return {'ok': False, 'error': '任务不存在'}
            item = next((it for it in job['items'] if it['status'] in ('error', 'done')), None) or (job['items'][0] if job['items'] else None)
            if item is None:
                return {'ok': False, 'error': '该任务没有可处理的条目'}
            video_path = str(item.get('save_path') or '')
            if not video_path or not os.path.exists(video_path):
                return {'ok': False, 'error': '视频文件不存在，无法补音频，请重新下载整个任务'}
        with self._parsed_lock:
            video_info = self._parsed.get(item['key'])
        if video_info is None:
            return {'ok': False, 'error': '解析缓存已失效，请回到主页重新解析链接后再补音频'}
        with self._jobs_lock:
            if job.get('status') not in ('done', 'error'):
                return {'ok': False, 'error': '该任务正在运行，无法补音频'}
            job['status'] = 'downloading'
            for it in job['items']:
                if it['key'] == item['key'] and it['status'] in ('done', 'error'):
                    it['status'] = 'downloading'; it['error'] = ''
            self._save_jobs()
        # Building the per-job client can trigger a multi-second engine import,
        # and _video_has_audio() runs a synchronous ffprobe. Both USED to run here
        # on the bridge thread — clicking 补音频 froze the window until they
        # finished. They now happen inside the worker (which also does the merge).
        threading.Thread(target=self._retry_audio_worker, args=(job_id, item['key'], video_path, video_info),
                         name=f'retryaudio-{job_id}', daemon=True).start()
        return {'ok': True, 'async': True}

    def _retry_audio_worker(self, job_id: str, item_key: str, video_path: str, video_info) -> None:
        source = str(getattr(video_info, 'source', '') or '')
        client = self._get_or_build_job_client(job_id, source)
        if client is None:
            with self._jobs_lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    item = next((it for it in job['items'] if it['key'] == item_key), None)
                    if item is not None:
                        item['status'] = 'error'; item['error'] = self._import_error or '引擎不可用，无法补音频'
                    self._update_job_status(job)
                    self._save_jobs()
            return
        # Nothing to do if the video already carries an audio track.
        if self._video_has_audio(video_path):
            with self._jobs_lock:
                live = self._jobs.get(job_id)
                if live is not None:
                    it = next((x for x in live['items'] if x['key'] == item_key), None)
                    if it is not None:
                        it['status'] = 'done'; it['error'] = ''
                    self._update_job_status(live)
                    self._save_jobs()
            return
        try:
            ok, msg = self._download_audio_and_merge(job_id, item_key, video_path, video_info, client)
        except Exception as err:
            ok, msg = False, str(err)
            self.log('error', f'[{job_id}] retry_audio crashed: {err}')
            self.log('debug', traceback.format_exc())
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            item = next((it for it in job['items'] if it['key'] == item_key), None)
            if item is not None:
                item['status'] = 'done' if ok else 'error'
                item['error'] = '' if ok else msg
            self._update_job_status(job)
            self._save_jobs()

    def _video_has_audio(self, path: str) -> bool:
        try:
            from vd.modules.utils.cmd import MergeVideoAudioCopyFFmpegCommand
            return bool(MergeVideoAudioCopyFFmpegCommand.hasaudiostream(path))
        except Exception:
            return False

    def _resolve_fresh_audio_url(self, video_info, client) -> str:
        '''For YouTube, refresh the audio googlevideo url via yt-dlp (n-decrypted),
        keyed by its itag, so the audio is not downloaded through the throttled
        raw `n=` url that caused the original failure.'''
        source = str(getattr(video_info, 'source', '') or '')
        if source != 'YouTubeVideoClient':
            return ''
        try:
            import re
            # identifier is "{vid}-{quality}"; quality labels never contain '-',
            # so rsplit is safe even when the video id itself contains '-'.
            vid = str(getattr(video_info, 'identifier', '') or '').rsplit('-', 1)[0]
            if not vid:
                return ''
            yt_client = self._sourceclient('YouTubeVideoClient', client)
            if yt_client is None or not hasattr(yt_client, '_resolve_via_ytdlp'):
                return ''
            mapping = yt_client._resolve_via_ytdlp(vid)
            if not mapping:
                return ''
            aurl = str(getattr(video_info, 'audio_download_url', '') or '')
            m = re.search(r'[?&]itag=(\d+)', aurl)
            itag = m.group(1) if m else ''
            if itag and itag in mapping:
                return mapping[itag]
            # no matching itag: the vid extraction (or the extraction itself) went
            # wrong — do NOT fall through to the throttled url below.
            self.log('warning', f'fresh audio url refresh returned no itag {itag} match (vid={vid[:12]})')
        except Exception as e:
            self.log('warning', f'refresh audio url via yt-dlp failed: {e}')
        return ''

    def _download_audio_and_merge(self, job_id: str, item_key: str, video_path: str, video_info, client) -> tuple:
        '''Download a fresh audio stream and mux it onto the existing video file.'''
        from vd.modules.utils.cmd import (
            MergeVideoAudioAudioTranscodeFFmpegCommand,
            MergeVideoAudioFullTranscodeFFmpegCommand,
            MergeVideoAudioCopyFFmpegCommand,
        )
        from vd.modules.utils.io import generateuniquetmppath
        from vd.modules.utils import VideoInfo

        source = str(getattr(video_info, 'source', '') or '')
        work_dir = self.config.work_dir
        audio_ext = str(getattr(video_info, 'audio_ext', '') or 'm4a') or 'm4a'
        audio_save_path = str(getattr(video_info, 'audio_save_path', '') or '')
        if not audio_save_path:
            audio_save_path = os.path.join(os.path.dirname(video_path), f'{Path(video_path).stem}.audio.{audio_ext}')
        # Kill any stale progress tasks left by a previous attempt (e.g. an audio
        # download that stalled at 98%) BEFORE starting — otherwise the UI stacks
        # the new progress bars on top of the old unfinished ones.
        try:
            self.bus.finish_tasks_for(job_id, item_key)
        except Exception:
            pass
        audio_url = str(getattr(video_info, 'audio_download_url', '') or '')
        fresh = self._resolve_fresh_audio_url(video_info, client)
        if fresh:
            audio_url = fresh
        elif source == 'YouTubeVideoClient' and 'n=' in audio_url:
            # the raw url still carries the encrypted throttle param and the
            # yt-dlp refresh failed — crawling it would stall at ~50% 0B/s.
            return False, '音频直链仍被 YouTube 限速（解密刷新失败），请稍后再点“补音频”'
        if not audio_url:
            return False, '缺少可用音频地址，无法补音频'
        audio_info = VideoInfo(
            source=source,
            download_url=audio_url,
            save_path=audio_save_path,
            ext=audio_ext,
            identifier=f'audio-retry-{getattr(video_info, "identifier", "") or item_key}',
            default_download_headers=getattr(video_info, 'default_audio_download_headers', None),
            default_download_cookies=getattr(video_info, 'default_audio_download_cookies', None),
        )
        self.bus.set_context(job_id, item_key)
        try:
            downloaded = self.downloadvideoinfo(audio_info, client) or []
        finally:
            self.bus.clear_context()
        if not downloaded:
            return False, '音频下载失败（可能仍被 YouTube 限速，请稍后切换网络再点“补音频”）'
        audio_file = downloaded[0].save_path
        ext = os.path.splitext(video_path)[1].lstrip('.') or 'mp4'
        tmp_out = generateuniquetmppath(dir=os.path.join(work_dir, source or 'vd'), ext=ext)
        merged = False
        for factory in (MergeVideoAudioAudioTranscodeFFmpegCommand, MergeVideoAudioFullTranscodeFFmpegCommand, MergeVideoAudioCopyFFmpegCommand):
            cmd = factory().build(video_file_path=video_path, audio_file_path=audio_file,
                                  output_file_path=tmp_out, mods=getattr(video_info, 'ffmpeg_settings', None))
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            except subprocess.CalledProcessError as err:
                self.log('warning', f'[{job_id}] merge via {factory.__name__} failed: {err}')
                continue
            if MergeVideoAudioCopyFFmpegCommand.hasaudiostream(tmp_out) or (not shutil.which('ffprobe')):
                merged = True
                break
        if not merged:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
            return False, '音频已下载，但合并失败（ffmpeg 报错）'
        backup = video_path + '.silentbak'
        try:
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(video_path, backup)
        except Exception:
            backup = None
        shutil.move(tmp_out, video_path)
        if backup and os.path.exists(backup):
            os.remove(backup)
        if os.path.exists(audio_file):
            os.remove(audio_file)
        self.log('info', f'[{job_id}] 已成功补录音频并重新合并: {video_path}')
        return True, '已成功补录音频并重新合并'

    def shutdown(self) -> None:
        '''Best-effort clean shutdown used when the window is closed.

        We must NOT cancel jobs here. Unfinished jobs are intentionally
        persisted to jobs.json so they are restored as PAUSED on the next launch
        and the user can resume them manually — cancelling would mark them
        "cancelled" and drop them from the restore. So shutdown only:
          * stops the executor (no new tasks scheduled),
          * kills any lingering child processes (ffmpeg/aria2c/node), and
          * flushes the current job state to disk.
        The process itself is terminated by the caller via os._exit, which also
        kills the WebView2 and engine child tree, so any still-running download
        thread is reaped without leaving orphans.'''
        try:
            with self._executor_lock:
                ex = self._executor
                self._executor = None
            if ex is not None:
                try:
                    ex.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
            _kill_tracked_subprocesses()
            # Persist current job state (including unfinished jobs) so the next
            # launch can restore them as paused. Do NOT cancel them.
            try:
                self._save_jobs()
            except Exception:
                pass
            diag.log('core', 'service shutdown: executor stopped, child processes killed, jobs left intact for restore')
        except Exception as err:
            diag.log('core', f'shutdown error (ignored): {err}', 'warning')

    '''-------------------- job persistence --------------------'''

    def _save_jobs(self) -> None:
        '''Persist the current jobs (metadata only) to jobs.json so paused /
        failed tasks survive an app restart. VideoInfo objects are NOT
        serializable, so only plain job/item metadata is stored; a resume after
        restart re-parses the source url to rebuild them (see resume()).'''
        try:
            with self._jobs_lock:
                data = [self._serialize_job(j) for j in self._jobs.values()]
            Config.jobspath().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as err:
            diag.log('core', f'failed to save jobs: {err}', 'warning')

    @staticmethod
    def _serialize_job(job: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'id': job['id'], 'status': job['status'], 'error': job.get('error', ''),
            'created_at': job.get('created_at', ''), 'started_at': job.get('started_at', ''),
            'finished_at': job.get('finished_at', ''), 'work_dir': job.get('work_dir', ''),
            'url': job.get('url', ''),
            'urls': job.get('urls', []) or ([job['url']] if job.get('url') else []),
            'done_count': job.get('done_count', 0),
            'total_count': job.get('total_count', len(job.get('items', []))),
            'sources_needed': job.get('sources_needed', []),
            'restored': job.get('restored', False),
            'items': [
                {'key': it['key'], 'title': it.get('title', ''), 'save_path': it.get('save_path', ''),
                 'status': it['status'], 'error': it.get('error', ''),
                 'subtask_count': it.get('subtask_count', 1), 'identifier': it.get('identifier', '')}
                for it in job.get('items', [])
            ],
        }

    def _load_jobs(self) -> None:
        '''Load persisted jobs at startup.

        Live download threads are gone after a restart, so every non-terminal
        job is restored as PAUSED — shown in the task list but NOT started. The
        user begins it manually via the per-job "开始/继续" button, which calls
        `resume()`; `resume()` re-parses the source url to rebuild the
        (non-serializable) VideoInfo and resumes from any existing partial file,
        so already-completed items stay done and only the missing ones are
        fetched. Terminal jobs (done / error / cancelled) are kept as-is for
        reference. This gives "restart -> unfinished tasks are back, but idle
        until the user explicitly starts them".'''
        path = Config.jobspath()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding='utf-8') or '[]')
        except Exception as err:
            diag.log('core', f'failed to read jobs.json: {err}', 'warning')
            return
        loaded = 0
        restored = 0
        with self._jobs_lock:
            for rec in raw:
                if not isinstance(rec, dict) or not rec.get('id'):
                    continue
                status = rec.get('status', 'queued')
                items = []
                for it in rec.get('items', []):
                    ist = it.get('status', 'queued')
                    sub_count = it.get('subtask_count', 1)
                    common = {
                        'key': it.get('key', ''), 'title': it.get('title', ''),
                        'save_path': it.get('save_path', ''), 'subtask_count': sub_count,
                        'identifier': it.get('identifier', ''),
                    }
                    if ist == 'done':
                        # Completed items stay done; only the missing ones are
                        # re-fetched when the user starts the job.
                        items.append({**common, 'status': 'done', 'error': it.get('error', '')})
                    else:
                        # Everything else resets to queued so a manual "start"
                        # re-submits only what is still missing.
                        items.append({**common, 'status': 'queued', 'error': ''})
                # Non-terminal jobs are restored as paused (NOT auto-started).
                restored_status = 'paused' if status not in ('done', 'error', 'cancelled') else status
                total_subtasks = sum(it.get('subtask_count', 1) for it in items)
                done_subtasks = sum(it.get('subtask_count', 1) for it in items if it['status'] == 'done')
                job = {
                    'id': rec['id'], 'items': items, 'status': restored_status, 'error': rec.get('error', ''),
                    'created_at': rec.get('created_at', ''), 'started_at': rec.get('started_at', ''),
                    'finished_at': rec.get('finished_at', ''), 'work_dir': rec.get('work_dir', self.config.work_dir),
                    'cancel_event': threading.Event(), 'pause_event': threading.Event(),
                    'done_count': done_subtasks, 'total_count': total_subtasks,
                    'url': rec.get('url', ''),
                    'urls': rec.get('urls', []) or ([rec['url']] if rec.get('url') else []),
                    'sources_needed': rec.get('sources_needed', []),
                    'restored': True,
                }
                self._jobs[job['id']] = job
                loaded += 1
                if restored_status == 'paused':
                    restored += 1
        if loaded:
            diag.log('core', f'loaded {loaded} persisted job(s): {restored} unfinished restored as paused, '
                              f'{loaded - restored} terminal kept for reference')
            self._save_jobs()

    '''-------------------- parsed cache persistence --------------------'''

    def _parsed_cache_path(self) -> Path:
        return Config.configpath().parent / 'parsed_cache.pkl'

    def _save_parsed_cache(self) -> None:
        '''Persist the in-memory VideoInfo objects that are still needed by
        loaded jobs (queued/paused/downloading/error) so retry/resume within
        the same session does not have to re-parse the source url.'''
        try:
            needed: Set[str] = set()
            with self._jobs_lock:
                for job in self._jobs.values():
                    if job['status'] in ('done', 'cancelled'):
                        continue
                    for it in job['items']:
                        if it['status'] not in ('done', 'cancelled'):
                            needed.add(it['key'])
            with self._parsed_lock:
                to_save = {k: self._parsed[k] for k in needed if k in self._parsed}
            path = self._parsed_cache_path()
            path.write_bytes(pickle.dumps(to_save, protocol=pickle.HIGHEST_PROTOCOL))
            diag.log('core', f'saved parsed cache: {len(to_save)} item(s)')
        except Exception as err:
            diag.log('core', f'failed to save parsed cache: {err}', 'warning')

    def _load_parsed_cache(self) -> None:
        try:
            path = self._parsed_cache_path()
            if not path.exists():
                return
            # VideoInfo pickles reference engine modules; make sure the engine source
            # is importable before unpickling so the cache can load successfully.
            if str(VD_SRC) not in sys.path and VD_SRC.exists():
                sys.path.insert(0, str(VD_SRC))
            with self._parsed_lock:
                data = pickle.loads(path.read_bytes())
                # Only restore cache entries that belong to jobs still loaded
                # after restart. Discarded unfinished jobs must not leave stale
                # VideoInfo objects in memory.
                needed: Set[str] = set()
                with self._jobs_lock:
                    for job in self._jobs.values():
                        for it in job.get('items', []):
                            needed.add(it.get('key'))
                self._parsed.update({k: v for k, v in data.items() if k in needed})
            diag.log('core', f'loaded parsed cache: {len(data)} item(s)')
        except Exception as err:
            diag.log('core', f'failed to load parsed cache: {err}', 'warning')

    def _cleanup_parsed_cache(self, job: Dict[str, Any]) -> None:
        '''Drop parsed VideoInfo entries that are no longer needed by any job.'''
        if not job:
            return
        with self._parsed_lock:
            for item in job.get('items', []):
                key = item.get('key')
                if key:
                    self._parsed.pop(key, None)
                    self._parsed_url.pop(key, None)

    def _remove_job(self, job_id: str) -> None:
        '''Remove a job and its parsed cache entries, then persist state.'''
        with self._jobs_lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return
        self._cleanup_parsed_cache(job)
        with self._job_clients_lock:
            self._job_clients.pop(job_id, None)
        self._save_jobs()
        self._save_parsed_cache()

    '''-------------------- state --------------------'''

    def _streams_done_for(self, job_id: str, item_key: str, snapshot: List[Dict[str, Any]]) -> int:
        '''How many of an in-flight item's streams (video/audio/segments) have
        fully downloaded, according to the live progress bus.

        The persisted ``done_count`` only advances when a whole ITEM finishes
        (video + audio + merge), which made the header sit at "0/2" for the
        entire download. Counting completed stream tasks gives the counter
        partial credit: video done -> "1/2", audio done too -> "2/2".

        `snapshot` is the progress bus snapshot, built ONCE by the caller (a
        long session rebuilds it on every download tick, and calling
        bus.snapshot() per item would rescan all history N times per poll).'''
        done = 0
        for t in snapshot:
            if t.get('job_id') != job_id or t.get('item_key') != item_key:
                continue
            if t.get('kind') not in ('download', 'audio', 'm3u8download'):
                continue
            total, completed = t.get('total'), t.get('completed') or 0
            try:
                if total and float(completed) >= float(total):
                    done += 1
            except Exception:
                continue
        return done

    def jobsnapshot(self, progress_snapshot: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if progress_snapshot is None:
            progress_snapshot = self.bus.snapshot()
        # Self-healing sweep: if a job was left in a transient state (cancelling /
        # pausing) but every item has reached a terminal status and no worker will
        # ever re-derive its status (e.g. the retry-audio thread died between the
        # download finishing and the status update), finalize it here so the UI
        # never sticks on 取消中/暂停中 forever. Only *transient* statuses are
        # touched; terminal ones are left untouched for the X-remove button.
        stale = []
        with self._jobs_lock:
            for job in self._jobs.values():
                if job.get('status') == 'cancelling' and job.get('cancel_event') and job['cancel_event'].is_set():
                    if all(it['status'] in ('cancelled', 'done', 'error') for it in job['items']):
                        job['status'] = 'cancelled'
                        job['finished_at'] = job.get('finished_at') or datetime.now().strftime('%H:%M:%S')
                        self._save_jobs()
                elif job.get('status') == 'pausing' and all(it['status'] in ('paused', 'done', 'error', 'cancelled') for it in job['items']):
                    job['status'] = 'paused'
                    self._save_jobs()
                elif job.get('status') in ('downloading',) :
                    # a job stuck 'downloading' with every item terminal and no
                    # live futures is equally dead (e.g. a crashed retry worker)
                    if all(it['status'] in ('done', 'error', 'cancelled') for it in job['items']):
                        stale.append(job)
            for job in stale:
                with self._futures_lock:
                    has_live = any(jid == job['id'] for jid, _ in self._futures.values())
                if not has_live:
                    job['status'] = 'error' if any(it['status'] == 'error' for it in job['items']) else 'done'
                    job['finished_at'] = job.get('finished_at') or datetime.now().strftime('%H:%M:%S')
                    self._save_jobs()
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        result = []
        for job in jobs:
            items_meta = [
                {
                    'key': i['key'], 'title': i['title'], 'save_path': i['save_path'],
                    'status': i['status'], 'error': i['error'],
                }
                for i in job['items']
            ]
            done_count = job.get('done_count', 0)
            if job.get('status') == 'downloading':
                # add partial credit from in-flight items (never above their
                # subtask_count, so retried stream tasks cannot inflate it;
                # finished items are already inside done_count)
                partial = 0
                for it in job['items']:
                    if it['status'] in ('queued', 'downloading', 'pausing', 'paused'):
                        partial += min(it.get('subtask_count', 1), self._streams_done_for(job['id'], it['key'], progress_snapshot))
                done_count = max(done_count, partial)
            result.append({
                'id': job['id'], 'status': job['status'], 'error': job['error'], 'work_dir': job['work_dir'],
                'created_at': job['created_at'], 'started_at': job['started_at'], 'finished_at': job['finished_at'],
                'done_count': done_count, 'total_count': job['total_count'],
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
        progress_snapshot = self.bus.snapshot()
        result = {
            'jobs': self.jobsnapshot(progress_snapshot), 'progress': progress_snapshot,
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
    def revealpath(path: str) -> Dict[str, Any]:
        '''打开文件所在目录并选中该文件（explorer /select）；目录或不存在时退化到打开目录。'''
        try:
            target = Path(path)
            if target.is_file():
                subprocess.Popen(['explorer', '/select,', str(target)])
                return {'ok': True}
            if target.is_dir():
                os.startfile(str(target))
                return {'ok': True}
            # 目标不存在时尝试打开所在目录（文件可能尚未生成或已被清理）
            parent = target.parent
            if parent.is_dir():
                os.startfile(str(parent))
                return {'ok': True}
            return {'ok': False, 'error': f'path does not exist: {path}'}
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
