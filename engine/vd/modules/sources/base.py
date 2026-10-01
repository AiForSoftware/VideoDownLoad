'''
Function:
    Implementation of BaseVideoClient
'''
import os
import re
import copy
import time
import m3u8
import random
import base64
import pickle
import shutil
import requests
import subprocess
from pathlib import Path
from rich.text import Text
from urllib.parse import urljoin
from fake_useragent import UserAgent
from platformdirs import user_log_dir
from m3u8.model import InitializationSection
from ..utils.youtubeutils import Stream as YouTubeStreamObj
from pathvalidate import sanitize_filepath, sanitize_filename
from ..utils.domains import obtainhostname, hostmatchessuffix
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Mapping, Optional, TYPE_CHECKING
from rich.progress import Progress, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn, TimeElapsedColumn, ProgressColumn, Task
from ..utils import touchdir, useparseheaderscookies, usedownloadheaderscookies, usesearchheaderscookies, cookies2dict, generateuniquetmppath, shortenpathsinvideoinfos, optionalimport, optionalimportfrom, cookies2string, safeunlinkpathobj, LoggerHandle, VideoInfo, FileTypeSniffer
from ..utils.cmd import MergeCCTVTsFilesFFmpegCommand, DownloadFromLocalTxtFileFFmpegCommand, DownloadWithFFmpegCommand, DownloadWithNM3U8DLRECommand, DownloadWithAria2cCommand, MergeVideoAudioAudioTranscodeFFmpegCommand, MergeVideoAudioCopyFFmpegCommand, MergeVideoAudioFullTranscodeFFmpegCommand, RemuxCopyFFmpegCommand, CommandBuilder


'''AutoRegisterMeta'''
import os as _os
import importlib as _importlib


class AutoRegisterMeta(type):
    '''Metaclass that registers every concrete VideoClient class into the
    matching `VideoClientBuilder` (sources) or `CommonVideoClientBuilder` (common)
    registry at class-creation time. Combined with the lazy `__init__.py`
    files, this lets the desktop app load parsers only when the user pastes a
    matching URL.

    Behaviour:
    - In the desktop backend (`VD_LAZY_PARSERS=1`), `__init__.py` does
      NOT eagerly import the parser modules. Each parser registers itself
      the first time its `.py` is loaded, on demand.
    - In the vd command-line tool (`VD_LAZY_PARSERS` unset), the
      `__init__.py` still calls the original eager-import list, so every
      parser is loaded up front; this metaclass just records each one in
      the same `REGISTERED_MODULES` dictionary as before.
    '''
    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)
        if name == 'BaseVideoClient':
            return cls
        if not name.endswith('VideoClient'):
            return cls
        module_path = str(namespace.get('__module__') or '')
        try:
            if '.common.' in module_path:
                builder_mod = _importlib.import_module('vd.modules.common')
                builder_cls = getattr(builder_mod, 'CommonVideoClientBuilder', None)
            else:
                builder_mod = _importlib.import_module('vd.modules.sources')
                builder_cls = getattr(builder_mod, 'VideoClientBuilder', None)
            if builder_cls is not None and name not in builder_cls.REGISTERED_MODULES:
                # write directly into the class-level dict (BaseModuleBuilder.register
                # is an instance method, but the registry is a class attribute).
                builder_cls.REGISTERED_MODULES[name] = cls
        except Exception:
            # never let the lazy-registration machinery crash a parser import
            pass
        return cls


'''VideoAwareColumn'''
class VideoAwareColumn(ProgressColumn):
    def __init__(self):
        super(VideoAwareColumn, self).__init__()
        self._download_col = DownloadColumn()
    '''render'''
    def render(self, task: Task):
        kind = task.fields.get("kind", "download")
        if kind == "overall": total = int(task.total) if task.total is not None else 0; return Text(f"{int(task.completed)}/{total} videos")
        elif kind == 'm3u8download': total = int(task.total) if task.total is not None else 0; return Text(f"{int(task.completed)}/{total} ts")
        else: return self._download_col.render(task)


'''BaseVideoClient'''
class BaseVideoClient(metaclass=AutoRegisterMeta):
    source = 'BaseVideoClient'
    LESHI_BASE64_ENCODE_PATTERN = re.compile(r'data:[^;]+;base64,([A-Za-z0-9+/=]+)')
    BILIBILI_REFERENCE_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36', 'Referer': 'https://www.bilibili.com/'}
    WEIBO_REFERENCE_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36', 'Referer': 'https://weibo.com/'}
    # 断流自动续传：一次 `_download` 允许的尝试次数，以及两次尝试之间的退避秒数。
    # 每次尝试都会重新读取 `.part` 的当前大小并从该断点发 Range 请求，所以"重试"
    # 就是真正的"续传"。可用环境变量 VD_RESUME_ATTEMPTS 覆盖次数。
    DOWNLOAD_RESUME_ATTEMPTS = max(1, int(os.environ.get('VD_RESUME_ATTEMPTS', '5') or 5))
    DOWNLOAD_RESUME_BACKOFF = (2, 5, 10, 20)
    # 外部下载器（ffmpeg / N_m3u8DL-RE / aria2c）没有字节级续传，重试=整条命令重跑，
    # 代价高，次数比 naive 少一些。
    EXTERNAL_CMD_ATTEMPTS = max(1, int(os.environ.get('VD_EXTERNAL_ATTEMPTS', '3') or 3))
    # ★ 慢速自动断线重连：持续 DOWNLOAD_STALL_WINDOW 秒低于 DOWNLOAD_STALL_SPEED（默认
    #   100KB/s）就主动掐断当前连接，交回重试循环发 Range 请求从断点续传（换连接常能
    #   绕开限速节点）。VD_STALL_SPEED=0 可关闭该检测。
    DOWNLOAD_STALL_SPEED = max(0, int(os.environ.get('VD_STALL_SPEED', str(100 * 1024)) or 0))
    DOWNLOAD_STALL_WINDOW = max(3, int(os.environ.get('VD_STALL_WINDOW', '10') or 10))
    # 重连频率限制：滑动 DOWNLOAD_RESUME_RATE_WINDOW 秒内最多 DOWNLOAD_RESUME_RATE_LIMIT
    # 次重连，超过就让等窗口腾出额度（避免"慢→掐→重连还是慢"空转打爆服务器）。
    DOWNLOAD_RESUME_RATE_LIMIT = max(1, int(os.environ.get('VD_RESUME_RATE_LIMIT', '2') or 2))
    DOWNLOAD_RESUME_RATE_WINDOW = max(10.0, float(os.environ.get('VD_RESUME_RATE_WINDOW', '60') or 60))
    # 慢速重连的总次数保护（频率限制之外的兜底，防止"服务器永远慢"时无限空转）。
    DOWNLOAD_STALL_RESUME_MAX = max(1, int(os.environ.get('VD_STALL_RESUME_MAX', '30') or 30))
    def __init__(self, auto_set_proxies: bool = False, random_update_ua: bool = False, enable_parse_curl_cffi: bool = False, enable_search_curl_cffi: bool = False, enable_download_curl_cffi: bool = False,
                 max_retries: int = 5, maintain_session: bool = False, logger_handle: LoggerHandle = None, disable_print: bool = False, work_dir: str = 'vd_outputs', freeproxy_settings: dict = None, 
                 default_search_cookies: dict = None, default_download_cookies: dict = None, default_parse_cookies: dict = None):
        # set up work dir
        touchdir(work_dir)
        # io attributes
        self.work_dir = work_dir
        # logging attributes
        self.disable_print = disable_print
        self.logger_handle = logger_handle if logger_handle else LoggerHandle()
        # http requests attributes
        self.max_retries = max(max_retries, 1)
        self.maintain_session = maintain_session
        # --proxies
        self.auto_set_proxies = auto_set_proxies
        self.freeproxy_settings = freeproxy_settings or {}
        freeproxy = optionalimportfrom('freeproxy', 'freeproxy')
        if TYPE_CHECKING: from freeproxy import freeproxy as freeproxy
        (default_freeproxy_settings := dict(disable_print=True, proxy_sources=['ProxiflyProxiedSession'], max_tries=20, init_proxied_session_cfg={})).update(self.freeproxy_settings)
        self.proxied_session_client = freeproxy.ProxiedSessionClient(**default_freeproxy_settings) if auto_set_proxies else None
        # --headers
        self.random_update_ua = random_update_ua
        self.default_search_headers = {'User-Agent': UserAgent().random}
        self.default_parse_headers = {'User-Agent': UserAgent().random}
        self.default_download_headers = {'User-Agent': UserAgent().random}
        self.default_headers = self.default_parse_headers
        # --cookies
        self.default_search_cookies = cookies2dict(default_search_cookies)
        self.default_download_cookies = cookies2dict(default_download_cookies)
        self.default_parse_cookies = cookies2dict(default_parse_cookies)
        self.default_cookies = self.default_parse_cookies
        # --curl-cffi
        self.enable_parse_curl_cffi = enable_parse_curl_cffi
        self.enable_search_curl_cffi = enable_search_curl_cffi
        self.enable_download_curl_cffi = enable_download_curl_cffi
        self.enable_curl_cffi = self.enable_parse_curl_cffi
        self.cc_impersonates = self._listccimpersonates() if (enable_parse_curl_cffi or enable_search_curl_cffi or enable_download_curl_cffi) else None
        # --init
        self._initsession()
    '''_listccimpersonates'''
    def _listccimpersonates(self):
        curl_cffi = optionalimport('curl_cffi')
        root, exts = Path(curl_cffi.__file__).resolve().parent, {".py", ".so", ".pyd", ".dll", ".dylib"}
        pat = re.compile(rb"\b(?:chrome|edge|safari|firefox|tor)(?:\d+[a-z_]*|_android|_ios)?\b")
        return sorted({m.decode("utf-8", "ignore") for p in root.rglob("*") if p.suffix in exts for m in pat.findall(p.read_bytes())})
    '''_initsession'''
    def _initsession(self):
        if self.maintain_session and getattr(self, 'session', None): self.session.headers = self.default_headers; return
        curl_cffi = optionalimport('curl_cffi')
        if TYPE_CHECKING: import curl_cffi as curl_cffi
        self.session = requests.Session() if not self.enable_curl_cffi else curl_cffi.requests.Session()
        self.session.headers = self.default_headers
    '''_ensureuniquefilepath'''
    def _ensureuniquefilepath(self, file_path: str):
        same_name_file_idx, unique_file_path = 1, sanitize_filepath(file_path)
        while os.path.exists(unique_file_path):
            directory, file_name = os.path.split(file_path); file_name_without_ext, ext = os.path.splitext(file_name)
            unique_file_path = os.path.join(directory, f"{file_name_without_ext} ({same_name_file_idx}){ext}"); same_name_file_idx += 1
        return unique_file_path
    '''_normalizemediapaths'''
    def _normalizemediapaths(self, video_info: VideoInfo) -> None:
        # `m4s` 是 DASH 裸流分片，落盘时统一换成 mp4 / m4a 容器。把这段改写收在一处，
        # 让 `_download` 与 `_downloadwithnaiveallinone` 的"已下载"判定看到的是同一个
        # 最终路径——否则一边判 `.m4s`、一边写 `.mp4`，恢复时会误判成"没下过"而整条重下。
        if video_info.ext in {'m4s'}: video_info.update(dict(ext='mp4', save_path=os.path.join(self.work_dir, self.source, f'{video_info.title}.mp4')))
        if video_info.audio_ext in {'m4s'}: video_info.update(dict(audio_ext='mp4', audio_save_path=os.path.join(self.work_dir, self.source, f'{video_info.title}.audio.m4a')))
    '''_sizeof'''
    @staticmethod
    def _sizeof(file_path) -> int:
        '''本地文件大小（不存在/异常一律 0），用于断点续传的起始偏移。'''
        try: return os.path.getsize(file_path) if os.path.exists(file_path) else 0
        except Exception: return 0
    '''_iscompletedownload'''
    @staticmethod
    def _iscompletedownload(file_path) -> bool:
        '''`file_path` 是不是一条**已完整下载**的成品。

        naive 下载器只在整条流写完之后才 `os.replace(part, 成品)`，所以
        "成品存在 + 非空 + 没有 .part 残留" 就等价于"上次已经下完了"。
        恢复下载时必须先做这个判断、再去 `_ensureuniquefilepath`，否则成品会被改名成
        "xxx (1).mp4"、.part 再也匹配不上，已下好的视频/音频会被整条重下一遍。
        '''
        if not file_path: return False
        try: return os.path.isfile(file_path) and os.path.getsize(file_path) > 0 and (not os.path.exists(f'{file_path}.part'))
        except Exception: return False
    '''_hasmergedaudio'''
    @staticmethod
    def _hasmergedaudio(file_path) -> bool:
        '''成品里是否已经带音轨（= 音视频合并已经成功，独立音频文件已被删除）。

        ffprobe 缺失时按"已合并"处理：宁可跳过，也绝不往一个已经合并好的文件里
        再塞第二条音轨。
        '''
        if not file_path or not os.path.isfile(file_path): return False
        if not shutil.which('ffprobe'): return True
        try: return MergeVideoAudioCopyFFmpegCommand.hasaudiostream(file_path)
        except Exception: return True
    '''_hassubtitlestream'''
    @staticmethod
    def _hassubtitlestream(file_path) -> bool:
        '''成品里是否已经内封了字幕轨。已封过就不再下载/二次封装——否则 ffmpeg 会往
        同一个文件里塞入重复的字幕流（断点续传、重复下载同一链接时都会命中）。'''
        if not file_path or not os.path.isfile(file_path): return False
        if not shutil.which('ffprobe'): return False
        try:
            cmd = ['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries', 'stream=index', '-of', 'csv=p=0', str(file_path)]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout or ''
            return bool(out.strip())
        except Exception: return False
    '''_iscontrolerror'''
    @staticmethod
    def _iscontrolerror(err) -> bool:
        '''用户暂停/取消是控制流，不是下载失败，绝不能被重试逻辑吞掉。'''
        return type(err).__name__ in ('DownloadPaused', 'DownloadCancelled')
    '''_isfatalhttpstatus'''
    @staticmethod
    def _isfatalhttpstatus(err) -> bool:
        '''4xx（超时/限流除外）重试多少次都不会成功，直接判死，别空耗退避时间。'''
        status = getattr(getattr(err, 'response', None), 'status_code', None)
        try: status = int(status)
        except Exception: return False
        return 400 <= status < 500 and status not in (408, 429)
    '''_sleepwithcontrolchecks'''
    def _sleepwithcontrolchecks(self, seconds, progress, task_id) -> None:
        '''退避等待期间也必须能被暂停/取消打断。

        引擎里唯一的控制检查点是 `progress.update()`（DesktopProgress.update →
        ProgressBus.check_controls 会抛出 DownloadPaused/DownloadCancelled），
        所以这里分片睡眠并周期性触发一次空更新，而不是一次 `time.sleep` 睡死。
        '''
        seconds = float(seconds or 0)
        if progress is None or task_id is None:
            time.sleep(seconds); return
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0: return
            progress.update(task_id, advance=0)
            time.sleep(min(0.25, remaining))
    '''_stallreconnectwait'''
    def _stallreconnectwait(self, resume_times: list) -> float:
        '''按"滑动窗口内最多 DOWNLOAD_RESUME_RATE_LIMIT 次"的重连频率限制，返回本次重连
        前还需等待的秒数。`resume_times` 存最近各次重连的时间戳，窗口外的顺手清掉。'''
        now = time.time()
        while resume_times and now - resume_times[0] > self.DOWNLOAD_RESUME_RATE_WINDOW: resume_times.pop(0)
        if len(resume_times) < self.DOWNLOAD_RESUME_RATE_LIMIT: return 0.0
        return max(0.0, self.DOWNLOAD_RESUME_RATE_WINDOW - (now - resume_times[0]))
    '''_runexternalcmd'''
    def _runexternalcmd(self, cmd: list, tag: str, progress: Progress | None = None, desc: str = '', attempts: int = None) -> bool:
        '''Run an external downloader (ffmpeg / N_m3u8DL-RE / aria2c) with retries.

        这些工具没有"从字节偏移续传"的能力，重试是整条命令重跑；但对付断网/握手这类
        瞬时故障已经足够——以前一次失败就直接判死整个条目。退避等待期间用
        `progress.update()` 触发控制检查，暂停/取消才不会失灵。
        '''
        attempts = int(attempts or self.EXTERNAL_CMD_ATTEMPTS)
        task_id = progress.add_task(desc or tag, total=None, kind="download") if progress is not None else None
        last_error = None
        try:
            for attempt in range(attempts):
                if attempt > 0:
                    _delay = self.DOWNLOAD_RESUME_BACKOFF[min(attempt - 1, len(self.DOWNLOAD_RESUME_BACKOFF) - 1)]
                    self.logger_handle.warning(f'{self.source}.{tag} >>> external command failed ({last_error}), retrying in {_delay}s (attempt {attempt+1}/{attempts})', disable_print=self.disable_print)
                    self._sleepwithcontrolchecks(_delay, progress, task_id)
                try:
                    subprocess.run(cmd, check=True, capture_output=(True if self.disable_print else False), text=True, encoding='utf-8', errors='ignore')
                    return True
                except subprocess.CalledProcessError as err:
                    last_error = err
            return False
        finally:
            if last_error is not None:
                stderr_tail = (getattr(last_error, 'stderr', None) or '')[-800:]
                self.logger_handle.error(f'{self.source}.{tag} >>> external command failed after {attempts} attempt(s) (Error: {last_error}; stderr: {stderr_tail})', disable_print=self.disable_print)
            if task_id is not None:
                try: progress.remove_task(task_id)
                except Exception: pass
    '''_search'''
    @usesearchheaderscookies
    def _search(self, keyword: str) -> list[VideoInfo]:
        raise NotImplementedError('not be implemented')
    '''search'''
    @usesearchheaderscookies
    def search(self, keyword: str) -> list[VideoInfo]:
        raise NotImplementedError('not be implemented')
    '''parsefromurl'''
    @useparseheaderscookies
    def parsefromurl(self, url: str, request_overrides: dict = None) -> list[VideoInfo]:
        raise NotImplementedError('not be implemented')
    '''_convertspecialdownloadurl'''
    def _convertspecialdownloadurl(self, download_url: str, tmp_file_name: str = None):
        # init
        is_converter_performed = False; touchdir(os.path.join(self.work_dir, self.source))
        # leshi base64 encoded url
        leshi_m = BaseVideoClient.LESHI_BASE64_ENCODE_PATTERN.match(download_url)
        if leshi_m and (not is_converter_performed):
            download_url, is_converter_performed = base64.b64decode(leshi_m.group(1)).decode("utf-8", errors="ignore"), True
            if not download_url.startswith('#EXTM3U'): return download_url, is_converter_performed
            tmp_file_path = os.path.join(self.work_dir, self.source, f'{tmp_file_name}.m3u8') if tmp_file_name else generateuniquetmppath(os.path.join(self.work_dir, self.source), ext='m3u8')
            with open(tmp_file_path, 'w') as fp: fp.write(download_url)
            return tmp_file_path, is_converter_performed
        # no matched known specifical urls
        return download_url, is_converter_performed
    '''_downloadfromyoutube'''
    @usedownloadheaderscookies
    def _downloadfromyoutube(self, video_info: VideoInfo, video_info_index: int = 0, downloaded_video_infos: list = [], request_overrides: dict = None, progress: Progress | None = None) -> list[VideoInfo]:
        # init
        if not video_info.with_valid_download_url: return downloaded_video_infos
        # 若成品已存在且大小与流一致，直接跳过，不重复下载（避免二次下载卡住/占用带宽）
        try:
            _cl = int(float(video_info.download_url.filesize or 0))
        except Exception:
            _cl = 0
        if _cl > 0 and os.path.isfile(video_info.save_path) and os.path.getsize(video_info.save_path) == _cl:
            downloaded_video_infos.append(video_info)
            return downloaded_video_infos
        (video_info := copy.deepcopy(video_info)).save_path = self._ensureuniquefilepath(video_info.save_path)
        request_overrides = dict(request_overrides or {}); touchdir(os.path.dirname(video_info.save_path))
        assert isinstance(video_info.download_url, YouTubeStreamObj)
        # start to download
        try:
            content_length, chunk_size = int(float(video_info.download_url.filesize or 0)), video_info.chunk_size
            _base = os.path.basename(video_info.save_path)
            # The audio stream is saved as `<title>.audio.<ext>`; tag it so the UI
            # can distinguish "downloading audio" from "downloading video".
            _is_audio = '.audio.' in _base
            _prefix = '音频 ' if _is_audio else ''
            desc_name = f"[{video_info_index+1}] {_prefix}{_base[:15] + '...'}" if len(_base) > 15 else f"[{video_info_index+1}] {_prefix}{_base[:15]}"
            total_bytes, downloaded_bytes = content_length if content_length > 0 else None, 0
            video_task_id = progress.add_task(desc_name, total=total_bytes, kind=("audio" if _is_audio else "download")) if progress is not None else None
            with open(video_info.save_path, "wb") as fp:
                for chunk in video_info.download_url.iterchunks(chunk_size=chunk_size):
                    if chunk: fp.write(chunk); downloaded_bytes += len(chunk)
                    if progress is not None:
                        total_bytes is None and progress.update(video_task_id, total=downloaded_bytes)
                        progress.update(video_task_id, advance=len(chunk))
            # Remove the completed task so it does not linger at 100% and skew the
            # item's aggregated progress bar (e.g. while the next phase runs).
            if progress is not None:
                progress.remove_task(video_task_id)
            downloaded_video_infos.append(video_info)
        except Exception as err:
            if type(err).__name__ in ('DownloadPaused', 'DownloadCancelled'):
                raise
            self.logger_handle.error(f'{self.source}._downloadfromyoutube >>> {video_info.identifier} (Error: {err})', disable_print=self.disable_print)
        # return
        return downloaded_video_infos
    '''_downloadfromcctv'''
    @usedownloadheaderscookies
    def _downloadfromcctv(self, video_info: VideoInfo, video_info_index: int = 0, downloaded_video_infos: list = [], request_overrides: dict = None, progress: Progress | None = None) -> list[VideoInfo]:
        # init
        if not video_info.with_valid_download_url: return downloaded_video_infos
        # ★ 成品已存在 → 直接复用，绝不二次下载（判定必须在 _ensureuniquefilepath 之前）。
        if self._iscompletedownload(video_info.save_path):
            self.logger_handle.info(f'{self.source}._downloadfromcctv >>> reuse already downloaded file: {video_info.save_path}', disable_print=self.disable_print)
            downloaded_video_infos.append(video_info)
            return downloaded_video_infos
        (video_info := copy.deepcopy(video_info)).save_path = self._ensureuniquefilepath(video_info.save_path)
        request_overrides = dict(request_overrides or {}); touchdir(os.path.dirname(video_info.save_path))
        if not request_overrides.get('proxies'): request_overrides['proxies'] = self._autosetproxies()
        ts_work_dir = sanitize_filepath(os.path.join(os.path.dirname(video_info.save_path), str(video_info.identifier)))
        # ★ 断点续传：保留 ts 分片目录（不再 rmtree），已下好的分片直接跳过，下次重跑
        #   只补缺失的分片，而不是从头再下整条 HLS。
        touchdir(ts_work_dir); video_info.identifier = sanitize_filename(str(video_info.identifier))
        node_script = Path(__file__).resolve().parents[2] / "modules" / "js" / "cctv" / "decrypt.js"
        # start to download
        loaded_m3u8_url, processed_files_fp = m3u8.load(video_info.download_url), open(os.path.join(ts_work_dir, f'{video_info.identifier}.txt'), 'w')
        desc_name = f"[{video_info_index+1}] {os.path.basename(video_info.save_path)[:15] + '...'}" if len(os.path.basename(video_info.save_path)) > 15 else f"[{video_info_index+1}] {os.path.basename(video_info.save_path)[:15]}"
        video_task_id = progress.add_task(desc_name, total=len(loaded_m3u8_url.segments), kind="m3u8download")
        for seg_idx, segment in enumerate(loaded_m3u8_url.segments):
            seg_path = os.path.join(ts_work_dir, f"segment_{seg_idx:08d}.mp4")
            # 已落盘且非空的分片视为已完成，跳过（断点续传核心）
            if os.path.isfile(seg_path) and os.path.getsize(seg_path) > 0:
                progress.update(video_task_id, advance=1); processed_files_fp.write(f"file 'segment_{seg_idx:08d}.mp4'\n"); continue
            cmd = ["node", node_script, segment.absolute_uri, seg_path]
            try: subprocess.run(cmd, check=True, capture_output=True, text=True, encoding='utf-8', errors='ignore'); progress.update(video_task_id, advance=1); processed_files_fp.write(f"file 'segment_{seg_idx:08d}.mp4'\n")
            except subprocess.CalledProcessError as err: self.logger_handle.error(f'{self.source}._downloadfromcctv >>> {segment.absolute_uri} (Error: {err})', disable_print=self.disable_print); progress.update(video_task_id, advance=1)
        processed_files_fp.close(); merge_ts_files_cmd = MergeCCTVTsFilesFFmpegCommand().build(video_info=video_info, ts_work_dir=ts_work_dir, mods=video_info.ffmpeg_settings)
        if self._runexternalcmd(merge_ts_files_cmd, '_downloadfromcctv', progress, f"合并 {os.path.basename(video_info.save_path)[:15]}"):
            shutil.rmtree(ts_work_dir, ignore_errors=True); downloaded_video_infos.append(video_info)
        # return
        return downloaded_video_infos
    '''_downloadfromdailymotion'''
    @usedownloadheaderscookies
    def _downloadfromdailymotion(self, video_info: VideoInfo, video_info_index: int = 0, downloaded_video_infos: list = [], request_overrides: dict = None, progress: Progress | None = None) -> list[VideoInfo]:
        # init
        if not video_info.with_valid_download_url: return downloaded_video_infos
        # ★ 成品已存在 → 直接复用，绝不二次下载（判定必须在 _ensureuniquefilepath 之前）。
        if self._iscompletedownload(video_info.save_path):
            self.logger_handle.info(f'{self.source}._downloadfromdailymotion >>> reuse already downloaded file: {video_info.save_path}', disable_print=self.disable_print)
            downloaded_video_infos.append(video_info)
            return downloaded_video_infos
        (video_info := copy.deepcopy(video_info)).save_path = self._ensureuniquefilepath(video_info.save_path)
        request_overrides = dict(request_overrides or {}); touchdir(os.path.dirname(video_info.save_path))
        # start to download
        try:
            (resp := self.get((download_url := video_info.download_url), **request_overrides)).raise_for_status()
            if (loaded_m3u8_obj := m3u8.loads(resp.content.decode("utf-8", errors="ignore"), uri=download_url)).playlists:
                download_url = (best := max(loaded_m3u8_obj.playlists, key=lambda p: p.stream_info.bandwidth or 0)).absolute_uri or urljoin(download_url, best.uri)
                (resp := self.get(download_url, **request_overrides)).raise_for_status(); loaded_m3u8_obj = m3u8.loads(resp.content.decode("utf-8", errors="ignore"), uri=download_url)
            desc_name = f"[{video_info_index+1}] {os.path.basename(video_info.save_path)[:15] + '...'}" if len(os.path.basename(video_info.save_path)) > 15 else f"[{video_info_index+1}] {os.path.basename(video_info.save_path)[:15]}"
            video_task_id, tmp_download_path, seen_init = progress.add_task(desc_name, total=len(loaded_m3u8_obj.segments), kind="m3u8download"), Path(video_info.save_path).with_suffix(".tmp.mp4"), set(); tmp_fp_obj = tmp_download_path.open('wb')
            for _, segment in enumerate(loaded_m3u8_obj.segments):
                init_section: InitializationSection = getattr(segment, "init_section", None)
                if init_section and init_section.uri and (init_url := (init_section.absolute_uri or urljoin(download_url, init_section.uri))) not in seen_init: tmp_fp_obj.write(self.get(init_url, **request_overrides).content); seen_init.add(init_url)
                tmp_fp_obj.write(self.get(segment.absolute_uri or urljoin(download_url, segment.uri), **request_overrides).content); progress.update(video_task_id, advance=1)
            tmp_fp_obj.close(); remux_copy_cmd = RemuxCopyFFmpegCommand().build(tmp_download_path, video_info.save_path, mods=video_info.ffmpeg_settings)
            if self._runexternalcmd(remux_copy_cmd, '_downloadfromdailymotion', progress, desc_name):
                safeunlinkpathobj(tmp_download_path, max_retries=20, delay=0.2); downloaded_video_infos.append(video_info)
        except Exception as err:
            if type(err).__name__ in ('DownloadPaused', 'DownloadCancelled'):
                raise
            self.logger_handle.error(f'{self.source}._downloadfromdailymotion >>> {video_info.download_url} (Error: {err})', disable_print=self.disable_print)
        # return
        return downloaded_video_infos
    '''_downloadfromlocaltxtfilewithffmpeg'''
    @usedownloadheaderscookies
    def _downloadfromlocaltxtfilewithffmpeg(self, video_info: VideoInfo, video_info_index: int = 0, downloaded_video_infos: list = [], request_overrides: dict = None, progress: Progress | None = None) -> list[VideoInfo]:
        # init
        if not video_info.with_valid_download_url: return downloaded_video_infos
        (video_info := copy.deepcopy(video_info)).save_path = self._ensureuniquefilepath(video_info.save_path)
        request_overrides = dict(request_overrides or {}); touchdir(os.path.dirname(video_info.save_path))
        if not request_overrides.get('proxies'): request_overrides['proxies'] = self._autosetproxies()
        default_headers = copy.deepcopy(video_info.default_download_headers or request_overrides.get('headers') or self.default_headers or {})
        default_cookies = copy.deepcopy(video_info.default_download_cookies or request_overrides.get('cookies') or self.default_cookies or {})
        if default_cookies: default_headers['cookie' if 'cookie' in default_headers else 'Cookie'] = cookies2string(default_cookies)
        # some pre-defined functions
        clean_func = lambda value: str(value).replace("\r", "").replace("\n", "").strip()
        ffconcat_quote_func = lambda value: "'" + ("" if value is None else str(value)).replace("'", r"'\''") + "'"
        is_special_header = lambda k: str(k).lower() == "user-agent" or str(k).lower() in ("referer", "referrer")
        build_ffconcat_options_func = lambda headers=None, proxies=None: (lambda hs, proxy_url: ([("user_agent" if k.lower() == "user-agent" else "referer", v) for k, v in hs if is_special_header(k)] + ([("headers", r"\r\n".join(f"{k}: {v}" for k, v in hs if not is_special_header(k)) + r"\r\n")] if any(not is_special_header(k) for k, _ in hs) else []) + ([("http_proxy", clean_func(proxy_url))] if proxy_url else [])))([(k, v) for k, v in ((clean_func(k), clean_func(v)) for k, v in (headers or {}).items() if k is not None and v is not None) if k], next(iter((proxies or {}).values()), None) if isinstance(proxies, dict) else None)
        # prepare txt file for ffmpeg to process
        with open(video_info.download_url, "r", encoding="utf-8") as fp: download_urls = [line.strip() for line in fp if line.strip()]
        original_file_path = video_info.download_url; video_info.download_url = video_info.download_url[:-4] + "_ffmpeg.txt"
        ffconcat_options = build_ffconcat_options_func(default_headers, request_overrides.get("proxies"))
        with open(video_info.download_url, "w", encoding="utf-8", newline="\n") as fp: fp.write("ffconcat version 1.0\n" + "".join(f"file {ffconcat_quote_func(download_url)}\n" + "".join(f"option {k} {ffconcat_quote_func(v)}\n" for k, v in ffconcat_options) for download_url in download_urls))
        # start to download
        cmd = DownloadFromLocalTxtFileFFmpegCommand().build(video_info=video_info, request_overrides=request_overrides, mods=video_info.ffmpeg_settings)
        try: subprocess.run(cmd, check=True, capture_output=(True if self.disable_print else False), text=True, encoding='utf-8', errors='ignore'); downloaded_video_infos.append(video_info); os.path.exists(video_info.download_url) and os.remove(video_info.download_url); os.path.exists(original_file_path) and os.remove(original_file_path)
        except subprocess.CalledProcessError as err: self.logger_handle.error(f'{self.source}._downloadfromlocaltxtfilewithffmpeg >>> {video_info.download_url} (Error: {err})', disable_print=self.disable_print)
        # return
        return downloaded_video_infos
    '''_downloadwithffmpeg'''
    @usedownloadheaderscookies
    def _downloadwithffmpeg(self, video_info: VideoInfo, video_info_index: int = 0, downloaded_video_infos: list = [], request_overrides: dict = None, progress: Progress | None = None) -> list[VideoInfo]:
        # init
        if not video_info.with_valid_download_url: return downloaded_video_infos
        (video_info := copy.deepcopy(video_info)).save_path = self._ensureuniquefilepath(video_info.save_path)
        request_overrides = dict(request_overrides or {}); touchdir(os.path.dirname(video_info.save_path))
        if not request_overrides.get('proxies'): request_overrides['proxies'] = self._autosetproxies()
        default_headers = copy.deepcopy(video_info.default_download_headers or request_overrides.get('headers') or self.default_headers or {})
        default_cookies = copy.deepcopy(video_info.default_download_cookies or request_overrides.get('cookies') or self.default_cookies or {})
        if default_cookies: default_headers['cookie' if 'cookie' in default_headers else 'Cookie'] = cookies2string(default_cookies)
        audio_default_headers = copy.deepcopy(video_info.default_audio_download_headers or request_overrides.get('headers') or self.default_headers or {})
        audio_default_cookies = copy.deepcopy(video_info.default_audio_download_cookies or request_overrides.get('cookies') or self.default_cookies or {})
        if audio_default_cookies: audio_default_headers['cookie' if 'cookie' in audio_default_headers else 'Cookie'] = cookies2string(audio_default_cookies)
        # some pre-defined functions
        build_ffmpeg_headers_option_func: Callable[[Optional[Mapping[str, Any]]], str] = lambda headers: ("" if not headers else (lambda clean: "".join(f"{clean(k)}: {clean(v)}\r\n" for k, v in headers.items() if k is not None and v is not None and clean(k)))(lambda x: str(x).replace("\r", "").replace("\n", "").strip()))
        # start to download
        header_opt, audio_header_opt = build_ffmpeg_headers_option_func(default_headers), build_ffmpeg_headers_option_func(audio_default_headers)
        cmd = DownloadWithFFmpegCommand().build(video_info=video_info, header_opt=header_opt, audio_header_opt=audio_header_opt, request_overrides=request_overrides, mods=video_info.ffmpeg_settings)
        _base = os.path.basename(video_info.save_path)
        desc_name = f"[{video_info_index+1}] {_base[:15] + '...'}" if len(_base) > 15 else f"[{video_info_index+1}] {_base[:15]}"
        # ffmpeg 会边下边写成品，中断留下的半截文件不能被 _iscompletedownload 当成成品，
        # 所以用 `<成品>.part` 占位标记"这条还没下完"。
        marker = f'{video_info.save_path}.part'
        try: open(marker, 'wb').close()
        except Exception: pass
        if self._runexternalcmd(cmd, '_downloadwithffmpeg', progress, desc_name):
            safeunlinkpathobj(marker)
            downloaded_video_infos.append(video_info)
        # return
        return downloaded_video_infos
    '''_downloadwithnm3u8dlre'''
    @usedownloadheaderscookies
    def _downloadwithnm3u8dlre(self, video_info: VideoInfo, video_info_index: int = 0, downloaded_video_infos: list = [], request_overrides: dict = None, progress: Progress | None = None) -> list[VideoInfo]:
        # init
        if not video_info.with_valid_download_url: return downloaded_video_infos
        # ★ 成品已存在 → 直接复用，绝不二次下载（判定必须在 _ensureuniquefilepath 之前）。
        if self._iscompletedownload(video_info.save_path):
            self.logger_handle.info(f'{self.source}._downloadwithnm3u8dlre >>> reuse already downloaded file: {video_info.save_path}', disable_print=self.disable_print)
            downloaded_video_infos.append(video_info)
            return downloaded_video_infos
        (video_info := copy.deepcopy(video_info)).save_path = self._ensureuniquefilepath(video_info.save_path)
        request_overrides = dict(request_overrides or {}); touchdir(os.path.dirname(video_info.save_path))
        if not request_overrides.get('proxies'): request_overrides['proxies'] = self._autosetproxies()
        default_headers = copy.deepcopy(video_info.default_download_headers or request_overrides.get('headers') or self.default_headers or {})
        default_cookies = copy.deepcopy(video_info.default_download_cookies or request_overrides.get('cookies') or self.default_cookies or {})
        if default_cookies: default_headers['cookie' if 'cookie' in default_headers else 'Cookie'] = cookies2string(default_cookies)
        # start to download
        log_file_path = generateuniquetmppath(dir=user_log_dir(appname='vd', appauthor='vd'), ext='log')
        # 断点续传：给 N_m3u8DL-RE 一个**稳定**的临时目录（默认 ./temp 每次都换，已下好
        # 的分片永远利用不上）。v0.6.0-beta 没有 --continue，但固定 tmp-dir 后重跑能复用
        # 已落盘的分片；--del-after-done 会在成功后清空它。
        tmp_dir = os.path.join(self.work_dir, '.vd_tmp', self.source, os.path.splitext(os.path.basename(video_info.save_path))[0])
        touchdir(tmp_dir)
        cmd = DownloadWithNM3U8DLRECommand().build(video_info=video_info, default_headers=default_headers, request_overrides=request_overrides, mods=video_info.nm3u8dlre_settings, log_file_path=log_file_path, tmp_dir=tmp_dir)
        _base = os.path.basename(video_info.save_path)
        desc_name = f"[{video_info_index+1}] {_base[:15] + '...'}" if len(_base) > 15 else f"[{video_info_index+1}] {_base[:15]}"
        if self._runexternalcmd(cmd, '_downloadwithnm3u8dlre', progress, desc_name):
            downloaded_video_infos.append(video_info); os.path.exists(video_info.download_url) and os.remove(video_info.download_url)
        # 清理已清空的临时目录（--del-after-done 已删内容，这里删空壳）
        try:
            if os.path.isdir(tmp_dir) and not os.listdir(tmp_dir): shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception: pass
        # return
        return downloaded_video_infos
    '''_downloadwitharia2c'''
    @usedownloadheaderscookies
    def _downloadwitharia2c(self, video_info: VideoInfo, video_info_index: int = 0, downloaded_video_infos: list = [], request_overrides: dict = None, progress: Progress | None = None) -> list[VideoInfo]:
        # init
        if not video_info.with_valid_download_url: return downloaded_video_infos
        raw = video_info.save_path
        # aria2c 的断点续传靠 "<成品>.aria2" 控制文件：成品一旦被改名成 "x (1).mp4"，
        # 控制文件就永远匹配不上，等于从头下。所以有半成品（成品 + .aria2）时保持原名不动。
        has_aria2 = os.path.exists(f'{raw}.aria2')
        if has_aria2:
            self.logger_handle.info(f'{self.source}._downloadwitharia2c >>> resuming partial via .aria2 control file: {raw}', disable_print=self.disable_print)
        elif self._iscompletedownload(raw):
            # ★ 成品已存在 → 直接复用，绝不二次下载（判定必须在 _ensureuniquefilepath 之前）。
            self.logger_handle.info(f'{self.source}._downloadwitharia2c >>> reuse already downloaded file: {raw}', disable_print=self.disable_print)
            downloaded_video_infos.append(video_info)
            return downloaded_video_infos
        (video_info := copy.deepcopy(video_info)).save_path = raw if has_aria2 else self._ensureuniquefilepath(raw)
        request_overrides = dict(request_overrides or {}); touchdir(os.path.dirname(video_info.save_path))
        if not request_overrides.get('proxies'): request_overrides['proxies'] = self._autosetproxies()
        default_headers = copy.deepcopy(video_info.default_download_headers or request_overrides.get('headers') or self.default_headers or {})
        default_cookies = copy.deepcopy(video_info.default_download_cookies or request_overrides.get('cookies') or self.default_cookies or {})
        if default_cookies: default_headers['cookie' if 'cookie' in default_headers else 'Cookie'] = cookies2string(default_cookies)
        # start to download
        cmd = DownloadWithAria2cCommand().build(video_info=video_info, default_headers=default_headers, request_overrides=request_overrides, mods=video_info.aria2c_settings)
        _base = os.path.basename(video_info.save_path)
        desc_name = f"[{video_info_index+1}] {_base[:15] + '...'}" if len(_base) > 15 else f"[{video_info_index+1}] {_base[:15]}"
        if self._runexternalcmd(cmd, '_downloadwitharia2c', progress, desc_name):
            downloaded_video_infos.append(video_info); os.path.exists(video_info.download_url) and os.remove(video_info.download_url)
        # return
        return downloaded_video_infos
    '''_downloadwithnaiveallinone'''
    @usedownloadheaderscookies
    def _downloadwithnaiveallinone(self, video_info: VideoInfo, video_info_index: int = 0, downloaded_video_infos: list = [], request_overrides: dict = None, progress: Progress | None = None) -> list[VideoInfo]:
        # init
        if not video_info.with_valid_download_url: return downloaded_video_infos
        # ★ 断点续传：先判两条流是否已经下完（用**原始**路径，在 _ensureuniquefilepath
        #   之前）。音频通常比视频先下完，一旦它被改名成 "xxx.audio (1).m4a"，.part 就
        #   再也匹配不上，恢复时会把已经下好的音频从头再下一遍。
        self._normalizemediapaths(video_info)
        video_done = self._iscompletedownload(video_info.save_path)
        audio_done = self._iscompletedownload(video_info.audio_save_path)
        # 视频成品在、独立音频文件不在，且成品里已经有音轨 ⇒ 上一次合并已经成功
        # （合并后会删掉音频文件）。整条直接复用，绝不重新下音频再二次合并。
        if video_done and not audio_done and self._hasmergedaudio(video_info.save_path):
            self.logger_handle.info(f'{self.source}._downloadwithnaiveallinone >>> reuse already merged file: {video_info.save_path}', disable_print=self.disable_print)
            downloaded_video_infos.append(video_info)
            return downloaded_video_infos
        (video_info := copy.deepcopy(video_info)).save_path = video_info.save_path if video_done else self._ensureuniquefilepath(video_info.save_path)
        if video_info.audio_save_path and not audio_done: video_info.audio_save_path = self._ensureuniquefilepath(video_info.audio_save_path)
        request_overrides = dict(request_overrides or {}); touchdir(os.path.dirname(video_info.save_path))
        if video_info.audio_save_path: touchdir(os.path.dirname(video_info.audio_save_path))
        # detach audio fields before dispatching downloads
        audio_download_url = video_info.pop('audio_download_url'); audio_save_path = video_info.pop('audio_save_path'); audio_ext = video_info.pop('audio_ext'); guess_audio_ext_result = video_info.pop('guess_audio_ext_result')
        audio_info = VideoInfo(
            source=video_info.source, download_url=audio_download_url, save_path=audio_save_path, ext=audio_ext, identifier=f'audio-{video_info.identifier}', guess_video_ext_result=guess_audio_ext_result,
            default_download_headers=video_info.default_audio_download_headers, default_download_cookies=video_info.default_audio_download_cookies
        )
        # download video and audio concurrently. Previously audio was downloaded
        # after video; a stuck audio stream would block completion and, if it
        # failed entirely, caused an IndexError when merging.
        def _streamreport(tag, infos, started, err):
            '''Emit one log line per stream so the UI log shows that the audio
            thread really ran, whether it succeeded, and at what speed.'''
            dur = max(time.time() - started, 0.001)
            size = 0
            for i in (infos or []):
                try: size += os.path.getsize(str(i.save_path))
                except Exception: pass
            if err is not None: state = f'FAILED ({err})'
            elif not infos: state = 'FAILED (no output file)'
            else: state = f'ok, {size / 1048576:.1f}MB in {dur:.1f}s ({size / 1048576 / dur:.2f} MB/s)'
            self.logger_handle.info(f'{self.source}._downloadwithnaiveallinone >>> {tag} stream: {state}', disable_print=self.disable_print)
        t_video, t_audio = time.time(), time.time()
        self.logger_handle.info(f'{self.source}._downloadwithnaiveallinone >>> starting video + audio downloads concurrently', disable_print=self.disable_print)
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_video = executor.submit(self._download, video_info=video_info, video_info_index=video_info_index, downloaded_video_infos=downloaded_video_infos, request_overrides=request_overrides, progress=progress)
            future_audio = executor.submit(self._download, video_info=audio_info, video_info_index=video_info_index, downloaded_video_infos=[], request_overrides=request_overrides, progress=progress)
            video_err = audio_err = None
            try:
                future_video.result()
            except BaseException as err:
                video_err = err; future_audio.cancel()
            try:
                downloaded_audio_infos = future_audio.result()
            except BaseException as err:
                audio_err = err; downloaded_audio_infos = []
        _streamreport('video', [dvi for dvi in downloaded_video_infos if (dvi.identifier == video_info.identifier)], t_video, video_err)
        _streamreport('audio', downloaded_audio_infos, t_audio, audio_err)
        # pause/cancel must abort the whole item; any other failure falls through
        # to the graceful handling below so one dead stream cannot lose the other
        for err in (video_err, audio_err):
            if err is not None and type(err).__name__ in ('DownloadPaused', 'DownloadCancelled'):
                raise err
        downloaded_video_info = [dvi for dvi in downloaded_video_infos if (dvi.identifier == video_info.identifier)]
        # If either stream failed, do not crash with IndexError; return what we
        # managed to download (usually the video-only file) so the user gets a
        # concrete result and a clear error log instead of a bare traceback.
        if not downloaded_video_info or not downloaded_audio_infos:
            missing_parts = []
            if not downloaded_video_info: missing_parts.append('video')
            if not downloaded_audio_infos: missing_parts.append('audio')
            self.logger_handle.error(f'{self.source}._downloadwithnaiveallinone >>> {"+".join(missing_parts)} download failed, skipping merge', disable_print=self.disable_print)
            if downloaded_video_info:
                downloaded_video_info[0].audio_download_url, downloaded_video_info[0].audio_save_path = audio_download_url, audio_save_path
                downloaded_video_info[0].audio_ext, downloaded_video_info[0].guess_audio_ext_result = audio_ext, guess_audio_ext_result
            return downloaded_video_infos
        # merge video and audio. Tracked as a packaging progress task so the UI
        # does not look frozen while ffmpeg muxes the two streams together.
        audio_save_path, audio_ext, video_save_path, ext = downloaded_audio_infos[0].save_path, downloaded_audio_infos[0].ext, downloaded_video_info[0].save_path, downloaded_video_info[0].ext
        _short = os.path.basename(video_save_path)[:15]
        _pkg_task = progress.add_task(f"合并/打包：{_short}", total=None, kind="packaging") if progress is not None else None
        try:
            file_path_for_merge_video_audio = generateuniquetmppath(dir=os.path.join(self.work_dir, self.source), ext=ext)
            merged_ok = False
            for merge_factory in (MergeVideoAudioAudioTranscodeFFmpegCommand, MergeVideoAudioFullTranscodeFFmpegCommand, MergeVideoAudioCopyFFmpegCommand):
                cmd = merge_factory().build(video_file_path=video_save_path, audio_file_path=audio_save_path, output_file_path=file_path_for_merge_video_audio, mods=video_info.ffmpeg_settings)
                try: subprocess.run(cmd, check=True, capture_output=(True if self.disable_print else False), text=True, encoding='utf-8', errors='ignore')
                except subprocess.CalledProcessError as err: self.logger_handle.error(f'{self.source}._downloadwithnaiveallinone >>> {video_info.download_url} (Error: {err})', disable_print=self.disable_print); continue
                if MergeVideoAudioCopyFFmpegCommand.hasaudiostream(file_path_for_merge_video_audio) or (not shutil.which('ffprobe')): merged_ok = True; break
            # 三种方案全失败时绝不能把（往往是空的）临时文件盖到已下好的视频上——那会
            # 直接毁掉成品。保留视频与音频两个文件，下次恢复/补音频时会自动重新合并。
            if merged_ok:
                shutil.move(file_path_for_merge_video_audio, video_save_path); os.path.exists(audio_save_path) and os.remove(audio_save_path)
            else:
                self.logger_handle.error(f'{self.source}._downloadwithnaiveallinone >>> merge failed for {video_save_path}, keeping the separate video/audio files for a later retry', disable_print=self.disable_print)
                safeunlinkpathobj(file_path_for_merge_video_audio)
        finally:
            if progress is not None:
                progress.remove_task(_pkg_task)
        # return
        downloaded_video_info[0].audio_download_url, downloaded_video_info[0].audio_save_path = audio_download_url, audio_save_path
        downloaded_video_info[0].audio_ext, downloaded_video_info[0].guess_audio_ext_result = audio_ext, guess_audio_ext_result
        return downloaded_video_infos
    '''_download'''
    @usedownloadheaderscookies
    def _download(self, video_info: VideoInfo, video_info_index: int = 0, downloaded_video_infos: list = [], request_overrides: dict = None, progress: Progress | None = None) -> list[VideoInfo]:
        # init
        if not video_info.with_valid_download_url: return downloaded_video_infos
        self._normalizemediapaths(video_info)
        judge_local_file_ext_func = lambda p: Path(str(p)).suffix[1:].lower() if p else ""
        # youtube video client
        if video_info.source in {'YouTubeVideoClient'} and isinstance(video_info.download_url, YouTubeStreamObj): return self._downloadfromyoutube(video_info=video_info, video_info_index=video_info_index, downloaded_video_infos=downloaded_video_infos, request_overrides=request_overrides, progress=progress)
        # cctv video client
        if video_info.source in {'CCTVVideoClient'} and video_info.get('hls_key') in {'hls_h5e_url'}: return self._downloadfromcctv(video_info=video_info, video_info_index=video_info_index, downloaded_video_infos=downloaded_video_infos, request_overrides=request_overrides, progress=progress)
        # dailymotion video client
        if video_info.source in {'DailyMotionVideoClient'}: return self._downloadfromdailymotion(video_info=video_info, video_info_index=video_info_index, downloaded_video_infos=downloaded_video_infos, request_overrides=request_overrides, progress=progress)
        # all in one downloader for downlowning both video and audio
        if video_info.with_valid_audio_download_url: return self._downloadwithnaiveallinone(video_info=video_info, video_info_index=video_info_index, downloaded_video_infos=downloaded_video_infos, request_overrides=request_overrides, progress=progress)
        # ffmpeg downloader for dealing with HLS urls / files
        valid_hls_exts_for_auto_set_ffpmeg, cannot_use_nm3u8dlre_sources = {'m3u8', 'm3u', 'mpd'}, {'XinpianchangVideoClient'}
        if any((video_info.ext.lower() in valid_hls_exts_for_auto_set_ffpmeg, FileTypeSniffer.pickextfromurl(video_info.download_url) in valid_hls_exts_for_auto_set_ffpmeg)): ext = video_info.ext if video_info.ext in {'mkv'} else 'mp4'; video_info.update(dict(ext=ext, download_with_ffmpeg=True, save_path=os.path.join(self.work_dir, self.source, f'{video_info.title}.{ext}')))
        no_nm3u8dlre_warnings = ('"enable_nm3u8dlre" has been set to True, but N_m3u8DL-RE was not found in the environment variables.' 'Please visit https://github.com/nilaoda/N_m3u8DL-RE to download and install the version of N_m3u8DL-RE that matches your system,' 'and then add it to your environment variables. Now, we will switch "enable_nm3u8dlre" to False and try downloading again.')
        # --from url or local hls files except for .txt file
        if video_info.download_with_ffmpeg and ((not os.path.exists(video_info.download_url)) or (os.path.exists(video_info.download_url) and (judge_local_file_ext_func(video_info.download_url) not in {'txt'}))):
            video_info.enable_nm3u8dlre = True if (shutil.which('N_m3u8DL-RE') and (video_info.get('enable_nm3u8dlre') is None) and (video_info.source not in cannot_use_nm3u8dlre_sources)) else video_info.enable_nm3u8dlre
            if video_info.enable_nm3u8dlre and (not shutil.which('N_m3u8DL-RE')): video_info.enable_nm3u8dlre = False; self.logger_handle.warning(f'{self.source}._download >>> {video_info.download_url} (Warning: {no_nm3u8dlre_warnings})', disable_print=self.disable_print)
            ext = video_info.ext if video_info.ext in {'mkv'} else 'mp4'; video_info.update(dict(ext=ext, download_with_ffmpeg=True, save_path=os.path.join(self.work_dir, self.source, f'{video_info.title}.{ext}')))
            return (self._downloadwithffmpeg(video_info=video_info, video_info_index=video_info_index, downloaded_video_infos=downloaded_video_infos, request_overrides=request_overrides, progress=progress) if (not video_info.enable_nm3u8dlre) else self._downloadwithnm3u8dlre(video_info=video_info, video_info_index=video_info_index, downloaded_video_infos=downloaded_video_infos, request_overrides=request_overrides, progress=progress))
        # --from local .txt file
        elif video_info.download_with_ffmpeg and os.path.exists(video_info.download_url) and (judge_local_file_ext_func(video_info.download_url) in {'txt'}):
            ext = video_info.ext if video_info.ext in {'mkv'} else 'mp4'; video_info.update(dict(ext=ext, download_with_ffmpeg=True, save_path=os.path.join(self.work_dir, self.source, f'{video_info.title}.{ext}')))
            return self._downloadfromlocaltxtfilewithffmpeg(video_info=video_info, video_info_index=video_info_index, downloaded_video_infos=downloaded_video_infos, request_overrides=request_overrides, progress=progress)
        # aria2c downloader for speeding up mp4 like files download
        if video_info.download_with_aria2c: return self._downloadwitharia2c(video_info=video_info, video_info_index=video_info_index, downloaded_video_infos=downloaded_video_infos, request_overrides=request_overrides, progress=progress)
        # naive implementition of file downloader with resume support
        # ★ 断点续传第一步：成品已存在就直接复用，绝不二次下载。判定必须在
        #   _ensureuniquefilepath 之前用**原始** save_path（理由见 _iscompletedownload）。
        if self._iscompletedownload(video_info.save_path):
            self.logger_handle.info(f'{self.source}._download >>> reuse already downloaded file: {video_info.save_path}', disable_print=self.disable_print)
            downloaded_video_infos.append(video_info)
            return downloaded_video_infos
        (video_info := copy.deepcopy(video_info)).save_path = self._ensureuniquefilepath(video_info.save_path)
        request_overrides = dict(request_overrides or {}); touchdir(os.path.dirname(video_info.save_path))
        if not request_overrides.get('proxies'): request_overrides['proxies'] = self._autosetproxies()
        request_overrides['headers'] = copy.deepcopy(video_info.default_download_headers or request_overrides.get('headers') or self.default_headers or {})
        request_overrides['cookies'] = copy.deepcopy(video_info.default_download_cookies or request_overrides.get('cookies') or self.default_cookies or {})
        # An audio stream (downloaded concurrently with the video one by
        # `_downloadwithnaiveallinone`) must be tagged as `audio`, otherwise
        # the UI folds both streams into a single "video" progress row and the
        # user cannot tell whether the audio is running/done at all.
        _base = os.path.basename(video_info.save_path)
        _is_audio = '.audio.' in _base or str(video_info.identifier or '').startswith('audio-')
        _prefix = '音频 ' if _is_audio else ''
        _name = _base[:15] + '...' if len(_base) > 15 else _base[:15]
        desc_name = f"[{video_info_index+1}] {_prefix}{_name}"
        # Download into a .part file first; if it already exists and the server
        # supports Range requests, resume from the existing size. This enables
        # pause/resume in the desktop UI and crash recovery.
        save_path, part_path = video_info.save_path, f'{video_info.save_path}.part'
        video_task_id, last_error = None, None
        # 两个独立预算：hard_fails（真断流/超时，上限 DOWNLOAD_RESUME_ATTEMPTS）和
        # stall_resumes（慢速触发的重连，上限 DOWNLOAD_STALL_RESUME_MAX、只受频率限制
        # 约束）——慢速重连不烧硬失败预算，否则持续慢速的服务器会把任务直接判死。
        # retries 是总重连次数，决定退避档位并参与频率限制。
        hard_fails, stall_resumes, retries, resume_times = 0, 0, 0, []
        try:
            # ★ 断流自动续传：每一轮尝试都重新读取 .part 的当前大小并从该断点发
            #   Range 请求，所以这里的"重试"就是"续传"，而不是整条重下。
            while True:
                if retries > 0:
                    _delay = self.DOWNLOAD_RESUME_BACKOFF[min(retries - 1, len(self.DOWNLOAD_RESUME_BACKOFF) - 1)]
                    # ★ 重连频率限制：滑动窗口内已用满额度就等窗口腾出名额。
                    _rate_wait = self._stallreconnectwait(resume_times)
                    if _rate_wait > _delay:
                        self.logger_handle.warning(f'{self.source}._download >>> reconnect rate limit ({self.DOWNLOAD_RESUME_RATE_LIMIT} per {self.DOWNLOAD_RESUME_RATE_WINDOW:.0f}s) hit, waiting another {_rate_wait:.0f}s', disable_print=self.disable_print)
                        _delay = _rate_wait
                    self.logger_handle.warning(f'{self.source}._download >>> stream interrupted ({last_error}), auto-resuming from {self._sizeof(part_path)} bytes in {_delay:.0f}s (resume #{retries}, stall {stall_resumes}/{self.DOWNLOAD_STALL_RESUME_MAX}, hard {hard_fails}/{self.DOWNLOAD_RESUME_ATTEMPTS})', disable_print=self.disable_print)
                    self._sleepwithcontrolchecks(_delay, progress, video_task_id)
                    resume_times.append(time.time())
                try:
                    start_byte = self._sizeof(part_path)
                    if start_byte > 0: request_overrides['headers']['Range'] = f'bytes={start_byte}-'
                    else: request_overrides['headers'].pop('Range', None)
                    resp = None
                    try:
                        resp = self.get(video_info.download_url, stream=True, **request_overrides)
                        if resp is None: raise requests.RequestException('empty response from server')
                        if start_byte > 0 and resp.status_code == 200:
                            # Server ignored Range; start from scratch.
                            start_byte = 0; request_overrides['headers'].pop('Range', None)
                            resp.close()
                            resp = self.get(video_info.download_url, stream=True, **request_overrides)
                            if resp is None: raise requests.RequestException('empty response from server')
                        elif start_byte > 0 and resp.status_code == 416:
                            # Range not satisfiable: .part is already complete.
                            resp.close(); os.replace(part_path, save_path)
                            downloaded_video_infos.append(video_info)
                            return downloaded_video_infos
                        resp.raise_for_status()
                    except Exception:
                        # 历史兜底：证书/握手类失败降级为不校验再取一次流。
                        # 注意必须把 start_byte 归零——不带 Range 的响应是完整内容，
                        # 若继续以 "ab" 追加会写出"半截旧内容 + 全量新内容"的坏文件。
                        try: resp is not None and resp.close()
                        except Exception: pass
                        start_byte = 0; request_overrides['headers'].pop('Range', None)
                        resp = self.get(video_info.download_url, stream=True, verify=False, **request_overrides)
                        if resp is None: raise requests.RequestException('empty response from server')
                        resp.raise_for_status()
                    content_length, chunk_size = int(float(resp.headers.get("Content-Length", 0) or 0)), video_info.chunk_size
                    if start_byte > 0 and resp.status_code == 206 and content_length > 0:
                        total_bytes = start_byte + content_length
                    elif content_length > 0:
                        total_bytes = content_length
                    else:
                        total_bytes = None
                    downloaded_bytes = start_byte
                    speed_samples = []
                    if video_task_id is None:
                        video_task_id = progress.add_task(desc_name, total=total_bytes, completed=start_byte, kind=("audio" if _is_audio else "download"))
                    else:
                        # rich 的 update(total=None) 语义是"保持不变"，所以总大小未知时
                        # 显式传 0，避免进度条沿用上一轮（可能已经失效）的 total。
                        progress.update(video_task_id, total=(total_bytes if total_bytes is not None else 0), completed=start_byte)
                    try:
                        with open(part_path, "ab" if start_byte > 0 else "wb") as fp:
                            for chunk in resp.iter_content(chunk_size=chunk_size):
                                if chunk:
                                    fp.write(chunk)
                                    downloaded_bytes += len(chunk)
                                    total_bytes is None and progress.update(video_task_id, total=downloaded_bytes)
                                    progress.update(video_task_id, advance=len(chunk))
                                    # ★ 慢速检测：滑动窗口内平均速度持续低于阈值，就主动
                                    #   掐断当前连接，交回重试循环发 Range 从断点续传（常能
                                    #   换到更快的节点/绕开限速）。重连频率受 1 分钟 2 次限制。
                                    # 注意剪枝要保留一条略超窗口的样本作基准（剪到只剩
                                    # 窗口内的样本会让" oldest >= window"永远差一点触发不了）。
                                    if self.DOWNLOAD_STALL_SPEED > 0:
                                        _now = time.time(); speed_samples.append((_now, downloaded_bytes))
                                        while len(speed_samples) > 2 and _now - speed_samples[1][0] > self.DOWNLOAD_STALL_WINDOW: speed_samples.pop(0)
                                        if _now - speed_samples[0][0] >= self.DOWNLOAD_STALL_WINDOW:
                                            _speed = (downloaded_bytes - speed_samples[0][1]) / (_now - speed_samples[0][0])
                                            if _speed < self.DOWNLOAD_STALL_SPEED:
                                                raise requests.RequestException(f'slow download: {_speed / 1024:.1f} KB/s < {self.DOWNLOAD_STALL_SPEED // 1024} KB/s for {self.DOWNLOAD_STALL_WINDOW}s, reconnecting to resume')
                    finally:
                        try: resp.close()
                        except Exception: pass
                    # 服务端提前断开时不能把半成品当成品落盘，交给重试循环从断点继续。
                    if total_bytes is not None and downloaded_bytes < total_bytes:
                        raise requests.RequestException(f'incomplete download: got {downloaded_bytes}/{total_bytes} bytes, the connection was closed early')
                    os.replace(part_path, save_path)
                    downloaded_video_infos.append(video_info)
                    return downloaded_video_infos
                except Exception as err:
                    # User pause/cancel are control flows, not download failures.
                    if self._iscontrolerror(err): raise
                    last_error = err
                    # 4xx（超时/限流除外）重试多少次都不会成功，别空耗退避时间。
                    if self._isfatalhttpstatus(err): break
                    # ★ 慢速触发的重连：不消耗硬失败预算，只受频率限制 + 总次数保护。
                    if 'slow download:' in str(err):
                        stall_resumes += 1
                        if stall_resumes > self.DOWNLOAD_STALL_RESUME_MAX:
                            self.logger_handle.error(f'{self.source}._download >>> stall-resume budget exhausted ({self.DOWNLOAD_STALL_RESUME_MAX} reconnects), giving up', disable_print=self.disable_print)
                            break
                    else:
                        hard_fails += 1
                        if hard_fails >= self.DOWNLOAD_RESUME_ATTEMPTS: break
                    retries += 1
            if last_error is not None: raise last_error
        except Exception as err:
            # User pause/cancel are control flows, not download failures.
            if self._iscontrolerror(err): raise
            self.logger_handle.error(f'{self.source}._download >>> {video_info.download_url} (Error: {err})', disable_print=self.disable_print)
        finally:
            # 收尾时摘掉进度任务：恢复/重试复用的是同一个任务，留在总线里会让 UI
            # 把多轮的字节数叠在一起。
            if video_task_id is not None:
                try: progress.remove_task(video_task_id)
                except Exception: pass
        # return
        return downloaded_video_infos
    '''_collect_subtitle_sources'''
    def _collect_subtitle_sources(self, video_info: VideoInfo, request_overrides: dict = None) -> list:
        # Build a deduplicated list of subtitle descriptors:
        #   {lang, url, ext, headers, cookies}
        # Sources: (1) explicitly set by a parser via video_info.subtitles, and
        # (2) automatically derived from HLS playlists (m3u8/m3u) so any HLS
        # source gets subtitles without per-parser wiring.
        subs = []
        for s in (video_info.get('subtitles') or []):
            if isinstance(s, dict) and s.get('url'):
                subs.append({
                    'lang': str(s.get('lang') or 'und'),
                    'url': str(s['url']),
                    'ext': str(s.get('ext') or 'vtt').lstrip('.').lower() or 'vtt',
                    'headers': s.get('headers') or {},
                    'cookies': s.get('cookies') or {},
                })
        if not subs and isinstance(video_info.download_url, str):
            ext = os.path.splitext(video_info.download_url)[1].lstrip('.').lower()
            if ext in ('m3u8', 'm3u'):
                try:
                    from ..utils.hls import TencentHLSHelper
                    _, hls_subs = TencentHLSHelper.naiveparsem3u8formats(video_info.download_url)
                    for lang, items in (hls_subs or {}).items():
                        for it in items:
                            if it.get('url'):
                                subs.append({'lang': str(lang), 'url': str(it['url']), 'ext': str(it.get('ext') or 'vtt').lstrip('.').lower() or 'vtt', 'headers': {}, 'cookies': {}})
                except Exception as err:
                    self.logger_handle.warning(f'{self.source}._collect_subtitle_sources >>> HLS subtitle parse failed (Error: {err})', disable_print=self.disable_print)
        seen, out = set(), []
        for s in subs:
            key = (s['lang'], s['url'])
            if key in seen: continue
            seen.add(key); out.append(s)
        return out

    '''_bilibili_json_to_vtt'''
    @staticmethod
    def _bilibili_json_to_vtt(data: dict) -> str:
        # B站 subtitle_url 返回的是专有 JSON（body 为 [{from,to,content,...}]），
        # 不是标准字幕格式，需在下载时转成 VTT 才能被 ffmpeg 封装。
        def _ts(t):
            t = float(t or 0)
            ms = int(round((t - int(t)) * 1000))
            h, m, s = int(t // 3600), int((t % 3600) // 60), int(t % 60)
            return f'{h:02d}:{m:02d}:{s:02d}.{ms:03d}'
        lines = ['WEBVTT', '']
        for seg in (data.get('body') or []):
            lines.append(f'{_ts(seg.get("from"))} --> {_ts(seg.get("to"))}')
            lines.append(str(seg.get('content') or '').replace('\\n', '\n'))
            lines.append('')
        return '\n'.join(lines)

    '''_download_subtitle_file'''
    def _download_subtitle_file(self, sub: dict, work_dir: str, request_overrides: dict = None):
        try:
            headers = copy.deepcopy(sub.get('headers') or {})
            cookies = copy.deepcopy(sub.get('cookies') or {})
            if cookies: headers['cookie' if 'cookie' in headers else 'Cookie'] = cookies2string(cookies)
            ro = dict(request_overrides or {})
            ro['headers'] = headers
            resp = self.get(sub['url'], stream=True, **ro)
            resp.raise_for_status()
            # B站专有 JSON 字幕：下载后转 VTT 再保存。
            if sub.get('format') == 'bilibili_json':
                import json as _json
                try:
                    data = _json.loads(resp.text)
                except Exception:
                    data = _json.loads(resp.content.decode('utf-8', errors='ignore'))
                vtt = self._bilibili_json_to_vtt(data)
                tmp = generateuniquetmppath(dir=work_dir, ext='vtt')
                with open(tmp, 'w', encoding='utf-8') as fp:
                    fp.write(vtt)
                return (sub['lang'], tmp)
            tmp = generateuniquetmppath(dir=work_dir, ext=sub['ext'] or 'vtt')
            with open(tmp, 'wb') as fp:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk: fp.write(chunk)
            return (sub['lang'], tmp)
        except Exception as err:
            self.logger_handle.error(f'{self.source}._download_subtitle_file >>> {sub.get("url")} (Error: {err})', disable_print=self.disable_print)
            return None

    '''_mux_subtitles_if_any'''
    def _mux_subtitles_if_any(self, video_info: VideoInfo, request_overrides: dict = None, progress: Progress | None = None) -> None:
        save_path = video_info.get('save_path') or ''
        if not save_path or not os.path.exists(save_path): return
        subs = self._collect_subtitle_sources(video_info, request_overrides)
        if not subs: return
        # ★ 成品里已经内封过字幕轨就不要再下载/封装一遍（断点续传、重复下载同一链接
        #   都会走到这里）：否则 ffmpeg 会往同一个文件里塞入一条重复的字幕流。
        if self._hassubtitlestream(save_path):
            self.logger_handle.info(f'{self.source}._mux_subtitles >>> subtitles already muxed, skipping: {save_path}', disable_print=self.disable_print)
            return
        work_dir = os.path.dirname(save_path) or '.'
        # Download subtitle files, tracked so the user can see this phase in the
        # progress bar instead of thinking the app froze after the video finished.
        _sub_task = progress.add_task(f"下载字幕：{os.path.basename(save_path)[:15]}", total=len(subs), kind="subtitle") if progress is not None else None
        downloaded = []
        for sub in subs:
            res = self._download_subtitle_file(sub, work_dir, request_overrides)
            if res: downloaded.append(res)
            if progress is not None:
                progress.update(_sub_task, advance=1)
        if progress is not None:
            progress.remove_task(_sub_task)
        if not downloaded:
            return
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.logger_handle.warning(f'{self.source}._mux_subtitles >>> ffmpeg not found, subtitles skipped', disable_print=self.disable_print)
            for _, p in downloaded: safeunlinkpathobj(p)
            return
        out_ext = os.path.splitext(save_path)[1].lstrip('.').lower() or 'mkv'
        out = generateuniquetmppath(dir=work_dir, ext=out_ext)
        builder = CommandBuilder(ffmpeg).flag('-y')
        # inputs MUST use -i: a bare positional is parsed by ffmpeg as an OUTPUT
        # file, which left the command with zero inputs and failed with
        # "Output file does not contain any stream" (subtitle mux never worked).
        builder.opt('-i', save_path)
        for _, p in downloaded: builder.opt('-i', p)
        builder.add('-map', '0')
        for i in range(len(downloaded)):
            builder.add('-map', str(i + 1))
        # Copy video/audio streams; for MP4 the subtitle side-data must be
        # transcoded to mov_text because WebVTT cannot be stored in MP4 as-is
        # (a bare `-c copy` would silently drop the subtitle stream). MKV/WebM
        # carry WebVTT natively, so they keep the copy.
        builder.add('-c', 'copy')
        if out_ext in ('mp4', 'm4v'):
            builder.add('-c:s', 'mov_text')
        for i, (lang, _) in enumerate(downloaded):
            builder.add(f'-metadata:s:s:{i}', f'language={lang}')
        builder.positional(out)
        # Mux the subtitles into the final video, tracked as a packaging phase.
        _mux_task = progress.add_task(f"封装字幕：{os.path.basename(save_path)[:15]}", total=None, kind="packaging") if progress is not None else None
        try:
            proc = subprocess.run(builder.tolist(), check=True, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            shutil.move(out, save_path)
        except subprocess.CalledProcessError as err:
            # include the ffmpeg stderr tail — without it a mux failure is
            # undebuggable (the temp inputs are deleted in the finally below)
            stderr_tail = (err.stderr or '')[-800:]
            self.logger_handle.error(f'{self.source}._mux_subtitles >>> {save_path} (Error: {err}; ffmpeg stderr: {stderr_tail})', disable_print=self.disable_print)
            os.path.exists(out) and os.remove(out)
        finally:
            if progress is not None:
                progress.remove_task(_mux_task)
            for _, p in downloaded: safeunlinkpathobj(p)

    '''download'''
    @usedownloadheaderscookies
    def download(self, video_infos: list[VideoInfo], num_threadings: int = 5, request_overrides: dict = None) -> list[VideoInfo]:
        # init
        if not (video_infos := [video_info for video_info in video_infos if video_info.with_valid_download_url]): return []
        request_overrides = dict(request_overrides or {})
        # The desktop passes the user's "download subtitles" preference through
        # request_overrides; pop it so it never reaches the network layer.
        download_subtitles = bool(request_overrides.pop('download_subtitles', True))
        downloaded_video_infos = []
        video_infos = shortenpathsinvideoinfos(video_infos, key='save_path'); video_infos = shortenpathsinvideoinfos(video_infos, key='audio_save_path')
        # logging
        self.logger_handle.info(f'Start to download videos using {self.source}.', disable_print=self.disable_print)
        # multi threadings for downloading videos
        with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(), VideoAwareColumn(), TransferSpeedColumn(), TimeElapsedColumn(), TimeRemainingColumn()) as progress:
            overall_task_id = progress.add_task("[bold cyan]Overall videos", total=len(video_infos), kind="overall")
            with ThreadPoolExecutor(max_workers=num_threadings) as executor:
                futures = [executor.submit(self._download, video_info, vid, downloaded_video_infos, request_overrides, progress) for vid, video_info in enumerate(video_infos)]
                for fut in as_completed(futures): fut.result(); progress.update(overall_task_id, advance=1)
            # The overall counter is only meaningful in the CLI console. In the
            # desktop UI it would otherwise attach to a single item and pollute
            # that item's aggregated progress bar (skewing it to 100% before the
            # merge/subtitle phases finish), so drop it here.
            progress.remove_task(overall_task_id)
        # download subtitles (if any) and mux them into the final videos
        if download_subtitles:
            for dvi in downloaded_video_infos:
                try: self._mux_subtitles_if_any(dvi, request_overrides, progress)
                except Exception as err: self.logger_handle.error(f'{self.source}._mux_subtitles >>> {getattr(dvi, "save_path", "")} (Error: {err})', disable_print=self.disable_print)
        # logging
        self.logger_handle.info(f'Finished downloading videos from {self.source}. Valid downloads: {len(downloaded_video_infos)}.', disable_print=self.disable_print)
        # return
        return downloaded_video_infos
    '''belongto'''
    @staticmethod
    def belongto(url: str, valid_domains: list[str] | set[str] = None):
        # set valid domains
        if valid_domains is None: valid_domains = {}
        # extract url domain
        domain = obtainhostname(url)
        # judge and return according to valid domains
        if not domain or not valid_domains: return False
        return hostmatchessuffix(domain, valid_domains)
    '''_autosetproxies'''
    def _autosetproxies(self):
        if not self.auto_set_proxies: return {}
        try: proxies = self.proxied_session_client.getrandomproxy()
        except Exception as err: self.logger_handle.error(f'{self.source}._autosetproxies >>> freeproxy lib failed to auto fetch proxies (Error: {err})', disable_print=self.disable_print); proxies = {}
        return proxies
    '''get'''
    def get(self, url, **kwargs):
        if 'cookies' not in kwargs: kwargs['cookies'] = self.default_cookies
        if 'impersonate' not in kwargs and self.enable_curl_cffi: kwargs['impersonate'] = random.choice(self.cc_impersonates)
        if 'timeout' not in kwargs:
            kwargs['timeout'] = 30  # never block forever on a stalled connection
        for _ in range(self.max_retries):
            if not self.maintain_session: self._initsession(); self.random_update_ua and self.session.headers.update({'User-Agent': UserAgent().random})
            # 注意：不能就地 kwargs.pop('proxies')——那样只有第一次重试会带代理，
            # 之后的重试会静默退化为直连（代理配置形同虚设）。这里每轮用副本。
            _kwargs = dict(kwargs)
            proxies = _kwargs.pop('proxies', None) or self._autosetproxies()
            resp = None
            try: resp = self.session.get(url, proxies=proxies, **_kwargs)
            except Exception as err: self.logger_handle.error(f'{self.source}.get >>> {url} (Error: {err})', disable_print=self.disable_print); continue
            if resp is not None and 400 <= resp.status_code < 500: self.logger_handle.warning(f'{self.source}.get >>> {url} (client error {resp.status_code}; not retrying)', disable_print=self.disable_print); resp.raise_for_status()
            try: resp.raise_for_status(); return resp
            except Exception as err: self.logger_handle.error(f'{self.source}.get >>> {url} (Error: {err}; status={resp.status_code if resp else None})', disable_print=self.disable_print); continue
        return resp
    '''post'''
    def post(self, url, **kwargs):
        if 'cookies' not in kwargs: kwargs['cookies'] = self.default_cookies
        if 'impersonate' not in kwargs and self.enable_curl_cffi: kwargs['impersonate'] = random.choice(self.cc_impersonates)
        if 'timeout' not in kwargs:
            kwargs['timeout'] = 30  # never block forever on a stalled connection
        for _ in range(self.max_retries):
            if not self.maintain_session: self._initsession(); self.random_update_ua and self.session.headers.update({'User-Agent': UserAgent().random})
            # 同 get()：每轮用副本，保证重试仍然走代理
            _kwargs = dict(kwargs)
            proxies = _kwargs.pop('proxies', None) or self._autosetproxies()
            resp = None
            try: resp = self.session.post(url, proxies=proxies, **_kwargs)
            except Exception as err: self.logger_handle.error(f'{self.source}.post >>> {url} (Error: {err})', disable_print=self.disable_print); continue
            if resp is not None and 400 <= resp.status_code < 500: self.logger_handle.warning(f'{self.source}.post >>> {url} (client error {resp.status_code}; not retrying)', disable_print=self.disable_print); resp.raise_for_status()
            try: resp.raise_for_status(); return resp
            except Exception as err: self.logger_handle.error(f'{self.source}.post >>> {url} (Error: {err}; status={resp.status_code if resp else None})', disable_print=self.disable_print); continue
        return resp
    '''_savetopkl'''
    def _savetopkl(self, data, file_path, auto_sanitize=True):
        if auto_sanitize: file_path = sanitize_filepath(file_path)
        with open(file_path, 'wb') as fp: pickle.dump(data, fp)