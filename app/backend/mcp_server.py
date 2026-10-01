'''
Function:
    MCP (Model Context Protocol) stdio server for VideoDownLoad.
    - reuses VideoDlService headless (no pywebview / no GUI / no window)
    - exposes 14 tools: list_sources, parse_url, parse_urls, download,
      get_download_state, pause_job, resume_job, cancel_job, retry_audio,
      clear_jobs, get_config, set_config, get_history, clear_history
    - stdio transport: stdin/stdout carry JSON-RPC only; every stray text
      write (rich progress bars, library warnings, engine prints) is
      forwarded to stderr so the protocol stream is never polluted.
Author:
    CodeBuddy
'''
from __future__ import annotations

import os
import sys
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---- sys.path bootstrap (same convention as app.py) ----
DESKTOP_ROOT = Path(__file__).resolve().parents[1]
if str(DESKTOP_ROOT) not in sys.path:
    sys.path.insert(0, str(DESKTOP_ROOT))

# force UTF-8 stdio on Windows before anything else imports / prints
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    except Exception:
        pass


class _StdoutGuard:
    '''sys.stdout replacement that keeps ``.buffer`` intact for the MCP
    transport while forwarding stray text prints to stderr.

    mcp's stdio_server() wraps ``sys.stdout.buffer`` when the server starts,
    so as long as the guard exposes the REAL binary buffer, the protocol
    stream stays clean; everything printed through the text layer (rich
    progress output, library warnings, engine prints...) is diverted to
    stderr instead of corrupting the JSON-RPC traffic.'''

    def __init__(self, real):
        self._real = real
        self.buffer = real.buffer

    def write(self, s):
        try:
            sys.stderr.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            sys.stderr.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


'''version (version.txt is the single source of truth, same as build_now.ps1)'''


def _readversion() -> str:
    try:
        return (DESKTOP_ROOT.parent / 'version.txt').read_text(encoding='utf-8').strip() or '1.0.0'
    except Exception:
        return '1.0.0'


'''lazy VideoDlService singleton (thread-safe; tools may be called concurrently)'''

_service = None
_service_lock = threading.Lock()


def getservice():
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                from backend.core import VideoDlService  # noqa: WPS433  (engine bootstrap is heavy)
                _service = VideoDlService(version=_readversion())
    return _service


def _warmup() -> None:
    '''Background engine pre-load: shaves the ~10s cold start off the first
    parse_url call. The desktop shell does the same on demand (api.sources()).'''
    try:
        getservice().ensureengine(wait=True)
    except Exception:
        pass


'''MCP server'''

from mcp.server.fastmcp import FastMCP  # noqa: E402  (import after sys.path/stdout setup)

mcp = FastMCP(
    'vd-downloader',
    instructions=(
        'VideoDownLoad (vd) MCP server: parse video urls from douyin / bilibili / '
        'youtube / tencent video (plus a generic web-media grabber fallback) and '
        'download them with pause/resume/cancel support. '
        'Workflow: 1) parse_url(url) -> quality items with unique `key`s; '
        '2) download(keys=[...]) -> job_id; 3) poll get_download_state(job_id) '
        'every few seconds until every item reaches a terminal status '
        '(done/error/cancelled/paused); the finished file path is in the item '
        'save_path. ffmpeg/ffprobe must be on PATH for merging; run list_sources '
        'to check tool availability.'
    ),
)


'''-------------------- tools: discovery --------------------'''


@mcp.tool()
def list_sources() -> Dict[str, Any]:
    '''List supported video platforms and environment tool availability.
    Call this first: it shows which parsers exist, whether the engine has
    finished loading, and whether ffmpeg/ffprobe/node/N_m3u8DL-RE/aria2c are
    installed (missing ffmpeg means video+audio merging will fail).'''
    service = getservice()
    try:
        service.ensureengine(wait=False)  # background load only, never block
    except Exception:
        pass
    platforms: List[str] = []
    generic: List[str] = []
    if service.engineready:
        # mirror api.sources(): use the parser-name tables (populated by
        # ensureengine) so ALL ~60 parsers are listed, not just touched ones.
        internal = {'BaseVideoClient', 'CommonVideoClient', 'BaseModuleBuilder'}
        platform_table = getattr(service, '_platform_parser_table', []) or []
        common_table = getattr(service, '_common_parser_table', []) or []
        platforms = sorted({cls for _, cls in platform_table if cls not in internal})
        generic = sorted({cls for _, cls in common_table if cls not in internal})
    if not platforms:
        platforms = list(service.source_names or [])
    if not generic:
        generic = list(service.common_source_names or [])
    return {
        'ok': True,
        'engine_ready': service.engineready,
        'engine_state': service.engine_state,
        'engine_version': service.engine_version,
        'engine_error': service.engineerror,
        'platforms': platforms,
        'generic': generic,
        'external_tools': {
            'ffmpeg': bool(shutil.which('ffmpeg')),
            'ffprobe': bool(shutil.which('ffprobe')),
            'node': bool(shutil.which('node')),
            'N_m3u8DL-RE': bool(shutil.which('N_m3u8DL-RE')),
            'aria2c': bool(shutil.which('aria2c')),
        },
    }


'''-------------------- tools: parse --------------------'''


@mcp.tool()
def parse_url(url: str) -> Dict[str, Any]:
    '''Parse one video url into downloadable quality items. Returns items each
    with a unique `key` (pass it to download), title, source, quality,
    has_audio, valid and save_path template. The FIRST call may take 10-60s
    (engine cold start); later calls are fast.'''
    service = getservice()
    return service.parse(str(url or '').strip())


@mcp.tool()
def parse_urls(urls: List[str]) -> Dict[str, Any]:
    '''Parse several video urls in one call. Merged items share the same shape
    as parse_url; per-url failures are listed in `errors` without aborting the
    rest of the batch.'''
    service = getservice()
    cleaned = [str(u).strip() for u in (urls or []) if str(u).strip()]
    return service.parse_batch(cleaned)


'''-------------------- tools: download control --------------------'''


@mcp.tool()
def download(keys: List[str], work_dir: Optional[str] = None) -> Dict[str, Any]:
    '''Start downloading previously parsed items by their `key`s (from
    parse_url / parse_urls). Each key usually maps to one quality tier.
    Returns a job_id used to track/control the download. Use get_download_state
    to poll progress.'''
    service = getservice()
    cleaned = [str(k).strip() for k in (keys or []) if str(k).strip()]
    return service.enqueue(cleaned, work_dir)


@mcp.tool()
def get_download_state(job_id: Optional[str] = None) -> Dict[str, Any]:
    '''Pure snapshot of download jobs and per-item live progress — returns
    immediately. Poll it every ~3-5s until every item status is terminal:
    done / error / cancelled / paused. Pass the job_id returned by download()
    to inspect a single job; omit it to see all jobs. Finished files land in
    the item save_path.'''
    service = getservice()
    progress = service.bus.snapshot()
    jobs = service.jobsnapshot(progress)
    if job_id:
        jid = str(job_id)
        jobs = [j for j in jobs if j.get('id') == jid]
        progress = [p for p in progress if p.get('job_id') == jid]
    return {'ok': True, 'jobs': jobs, 'progress': progress}


@mcp.tool()
def pause_job(job_id: str) -> Dict[str, Any]:
    '''Pause a running download job (its items will stop at the next chunk).
    Resume later with resume_job.'''
    return getservice().pause(str(job_id or ''))


@mcp.tool()
def resume_job(job_id: str) -> Dict[str, Any]:
    '''Resume a paused download job. The naive downloader keeps a `<name>.part`
    file and re-issues a `Range: bytes=N-` request from its current size, so
    paused/interrupted items continue where they left off instead of restarting.
    (ffmpeg / N_m3u8DL-RE / aria2c based downloads still restart from zero.)'''
    return getservice().resume(str(job_id or ''))


@mcp.tool()
def cancel_job(job_id: str) -> Dict[str, Any]:
    '''Cancel a download job and stop its running child processes
    (ffmpeg/aria2c/node).'''
    return getservice().cancel(str(job_id or ''))


@mcp.tool()
def retry_audio(job_id: str) -> Dict[str, Any]:
    '''Retry the audio track for a finished item whose video has no sound
    (e.g. the audio url expired during merge).'''
    return getservice().retry_audio(str(job_id or ''))


@mcp.tool()
def clear_jobs() -> Dict[str, Any]:
    '''Remove finished/cancelled/failed jobs from the job list.'''
    return getservice().clearjobs()


'''-------------------- tools: config & history --------------------'''


@mcp.tool()
def get_config() -> Dict[str, Any]:
    '''Read the current downloader configuration (work_dir, concurrency,
    proxy, cookies, default quality, subtitle behaviour, allowed parsers).'''
    cfg = getservice().config
    return {
        'ok': True,
        'config': {
            'work_dir': cfg.work_dir,
            'num_threadings': cfg.num_threadings,
            'concurrent_downloads': cfg.concurrent_downloads,
            'proxy': cfg.proxy,
            'cookies': cfg.cookies,
            'per_source_cookies': dict(cfg.per_source_cookies or {}),
            'default_quality': cfg.default_quality,
            'apply_common_clients_only': cfg.apply_common_clients_only,
            'download_subtitles': cfg.download_subtitles,
            'allowed_sources': list(cfg.allowed_sources or []),
        },
    }


@mcp.tool()
def set_config(
    work_dir: Optional[str] = None,
    num_threadings: Optional[int] = None,
    concurrent_downloads: Optional[int] = None,
    proxy: Optional[str] = None,
    cookies: Optional[str] = None,
    default_quality: Optional[str] = None,
    download_subtitles: Optional[bool] = None,
    apply_common_clients_only: Optional[bool] = None,
    allowed_sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    '''Update downloader configuration (only the fields you pass are changed).
    Validation mirrors the desktop app: concurrent_downloads 1-16,
    num_threadings 1-32, default_quality one of best/4k/1080p/720p/480p/360p/auto.
    work_dir must be an existing directory (the engine writes files there).'''
    service = getservice()
    cfg = service.config
    if work_dir is not None and str(work_dir).strip():
        cfg.work_dir = str(work_dir).strip()
    if num_threadings is not None:
        try:
            cfg.num_threadings = max(1, min(32, int(num_threadings)))
        except Exception:
            pass
    if concurrent_downloads is not None:
        try:
            cfg.concurrent_downloads = max(1, min(16, int(concurrent_downloads)))
        except Exception:
            pass
    if proxy is not None:
        cfg.proxy = str(proxy or '').strip()
    if cookies is not None:
        cfg.cookies = str(cookies or '').strip()
    if default_quality is not None:
        cfg.default_quality = str(default_quality or 'best').strip().lower() or 'best'
    if download_subtitles is not None:
        cfg.download_subtitles = bool(download_subtitles)
    if apply_common_clients_only is not None:
        cfg.apply_common_clients_only = bool(apply_common_clients_only)
    if allowed_sources is not None:
        cfg.allowed_sources = [str(s) for s in allowed_sources if str(s).strip()]
    cfg.save()
    # client rebuild is expensive (seconds on a cold engine) — do it in the
    # background exactly like the desktop bridge (api.setconfig) does.
    def _rebuild():
        try:
            service._buildclient(force=True)
        except Exception:
            pass
    threading.Thread(target=_rebuild, name='mcp-rebuild-client', daemon=True).start()
    service.log('info', 'settings saved via mcp')
    return get_config()


@mcp.tool()
def get_history() -> Dict[str, Any]:
    '''Return the persistent parse history (most recent first).'''
    return {'ok': True, 'history': list(getservice().history)}


@mcp.tool()
def clear_history() -> Dict[str, Any]:
    '''Clear the persistent parse history.'''
    from backend.core import HistoryStore  # noqa: WPS433
    service = getservice()
    service.history = HistoryStore.clear()
    return {'ok': True}


'''-------------------- entry --------------------'''


def run() -> int:
    '''Run the MCP stdio server (blocks until the client closes stdin).'''
    # install the stdout guard BEFORE FastMCP starts: stdio_server() wraps
    # sys.stdout.buffer at startup, so the guard must already be in place
    # while still exposing the REAL buffer for the transport.
    try:
        sys.stdout.flush()
    except Exception:
        pass
    if getattr(sys.stdout, 'buffer', None) is not None:
        sys.stdout = _StdoutGuard(sys.stdout)
    # pre-load the engine in the background so the first parse is fast
    threading.Thread(target=_warmup, name='mcp-warmup', daemon=True).start()
    try:
        mcp.run(transport='stdio')
    finally:
        # kill any orphaned ffmpeg/aria2c/node children on shutdown
        try:
            if _service is not None:
                _service.shutdown()
        except Exception:
            pass
    return 0
