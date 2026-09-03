'''
Function:
    Implementation of YouTubeVideoClient
'''
import os
import re
import json
import time
from contextlib import suppress
from .base import BaseVideoClient
from ..utils.youtubeutils import YouTube, RequestWrapper
from urllib.parse import parse_qs, urlparse
from ..utils import legalizestring, yieldtimerelatedtitle, safeextractfromdict, VideoInfo, useparseheaderscookies


'''YouTubeVideoClient'''
class YouTubeVideoClient(BaseVideoClient):
    source = 'YouTubeVideoClient'
    def __init__(self, **kwargs):
        super(YouTubeVideoClient, self).__init__(**kwargs)
        self.default_parse_headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"}
        self.default_download_headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"}
        self.default_headers = self.default_parse_headers
        self._initsession()

    '''_fetch_via_browser: DrissionPage fallback when all Innertube clients are blocked'''
    def _fetch_via_browser(self, url: str, vid: str) -> dict:
        '''Open the YouTube watch page in a real Chromium browser and extract
        ytInitialPlayerResponse from the page. This bypasses all Innertube
        fingerprint/IP checks because the request comes from a real browser.'''
        from vd.modules.utils.chromium import DrissionPageUtils
        self.logger_handle.info(f'[DIAG youtube] browser fetch START url={url}', disable_print=self.disable_print)
        page = None
        try:
            browser_path = DrissionPageUtils.findsystembrowser()
            if not browser_path:
                self.logger_handle.warning('[DIAG youtube] no system Chrome/Edge found; skipping browser fallback', disable_print=self.disable_print)
                return {}
            page = DrissionPageUtils.initsmartbrowser(
                headless=True,
                requests_proxies=self._autosetproxies(),
                browser_path=browser_path,
                allow_download=False,
            )
            page.get(url)
            # Wait for JS to render ytInitialPlayerResponse
            time.sleep(5)
            # Extract ytInitialPlayerResponse from the page
            raw = page.run_js('return window.ytInitialPlayerResponse ? JSON.stringify(window.ytInitialPlayerResponse) : "";')
            if raw and isinstance(raw, str):
                data = json.loads(raw)
                ps = data.get('playabilityStatus', {})
                self.logger_handle.info(f'[DIAG youtube] browser extract playability={ps.get("status")}', disable_print=self.disable_print)
                return data
            # Fallback: parse from HTML
            html = page.html
            match = re.search(r'var ytInitialPlayerResponse\s*=\s*(\{.+?\});', html, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                ps = data.get('playabilityStatus', {})
                self.logger_handle.info(f'[DIAG youtube] browser HTML extract playability={ps.get("status")}', disable_print=self.disable_print)
                return data
        except Exception as err:
            self.logger_handle.error(f'[DIAG youtube] browser fetch failed: {err}', disable_print=self.disable_print)
        finally:
            if page:
                with suppress(Exception): DrissionPageUtils.quitpage(page)
        return {}

    '''parsefromurl'''
    @useparseheaderscookies
    def parsefromurl(self, url: str, request_overrides: dict = None) -> list[VideoInfo]:
        # prepare
        if not self.belongto(url=url): return []
        request_overrides, video_info, null_backup_title = request_overrides or {}, VideoInfo(source=self.source), yieldtimerelatedtitle(self.source)
        # extract video id
        parsed = urlparse(url)
        vid_list = parse_qs(parsed.query, keep_blank_values=True).get('v')
        if not vid_list:
            # Try to extract from URL path (youtu.be/ID or /embed/ID)
            path_parts = parsed.path.strip('/').split('/')
            if path_parts:
                vid_list = [path_parts[-1]]
        if not vid_list:
            video_info.update(dict(err_msg=f'{self.source}.parsefromurl >>> {url} (Error: could not extract video id)'))
            return [video_info]
        vid = vid_list[0]
        # try parse with official Innertube API (curl_cffi TLS impersonation)
        raw_data = {}
        try:
            yt = YouTube(video_id=vid)
            raw_data = yt.vid_info
            playability = raw_data.get('playabilityStatus', {})
            # Default to ERROR if playabilityStatus is missing (e.g. error response)
            status = playability.get('status', 'ERROR')
            has_streams = bool(raw_data.get('streamingData', {}).get('formats') or raw_data.get('streamingData', {}).get('adaptiveFormats'))
            if status != 'OK' or not has_streams:
                # All Innertube clients failed, try browser fallback
                self.logger_handle.info(f'[DIAG youtube] Innertube blocked ({status}), trying browser fallback', disable_print=self.disable_print)
                raw_data = self._fetch_via_browser(url, vid)
                playability = raw_data.get('playabilityStatus', {})
                status = playability.get('status', 'UNKNOWN')
                if status != 'OK':
                    reason = playability.get('reason', 'YouTube requires verification')
                    raise RuntimeError(f'YouTube blocked all request methods: {status} - {reason}. Try enabling a proxy.')
            # extract streams
            stream = yt.streams.gethighestresolution()
            download_url = stream.url if stream else ''
            video_info.update(dict(download_url=download_url))
            # if download_url is empty, try extracting from browser raw_data
            if not download_url and raw_data.get('streamingData'):
                download_url = self._extract_best_stream(raw_data['streamingData'])
                video_info.update(dict(download_url=download_url))
            video_title = legalizestring(raw_data.get('videoDetails', {}).get('title') or yt.title, replace_null_string=null_backup_title).removesuffix('.')
            cover_url = safeextractfromdict(raw_data, ['videoDetails', 'thumbnail', 'thumbnails', -1, 'url'], None)
            video_info.update(dict(title=video_title, save_path=os.path.join(self.work_dir, self.source, f'{video_title}.mp4'), ext='mp4', identifier=vid, cover_url=cover_url))
        except Exception as err:
            video_info.update(dict(err_msg=(err_msg := f'{self.source}.parsefromurl >>> {url} (Error: {err})')))
            self.logger_handle.error(err_msg, disable_print=self.disable_print)
        # return
        return [video_info]

    '''_extract_best_stream: pick highest quality URL from streamingData'''
    @staticmethod
    def _extract_best_stream(streaming_data: dict) -> str:
        formats = streaming_data.get('formats', [])
        adaptive = streaming_data.get('adaptiveFormats', [])
        # Prefer progressive (muxed) streams
        for f in formats:
            if 'url' in f: return f['url']
        # Pick highest resolution adaptive video
        video_streams = [f for f in adaptive if 'video' in f.get('mimeType', '') and 'url' in f]
        video_streams.sort(key=lambda x: int(x.get('height', 0)), reverse=True)
        if video_streams: return video_streams[0]['url']
        # Pick highest bitrate audio
        audio_streams = [f for f in adaptive if 'audio' in f.get('mimeType', '') and 'url' in f]
        audio_streams.sort(key=lambda x: int(x.get('bitrate', 0)), reverse=True)
        if audio_streams: return audio_streams[0]['url']
        return ''

    '''belongto'''
    @staticmethod
    def belongto(url: str, valid_domains: list[str] | set[str] = None):
        valid_domains = set(valid_domains or []) | {"youtube.com", "youtu.be"}
        return BaseVideoClient.belongto(url, valid_domains)
