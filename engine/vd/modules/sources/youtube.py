'''
Function:
    Implementation of YouTubeVideoClient
Author:
    CodeBuddy
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


'''YouTubeVideoClient

Design notes (learned from Tyrrrz/YoutubeDownloader):
    1. multi-client InnerTube rotation: the ANDROID client family does not require
       poToken and returns PLAIN stream urls (require_js_player=False), so it is the
       primary source. WEB-family responses are signatureCipher-encrypted and are
       only reachable through the browser fallback.
    2. quality enumeration: emit one VideoInfo per distinct quality label —
       progressive (muxed, audio included) for <=720P, adaptive video + best audio
       (merged by the downloader via ffmpeg) for >=1080P. Titles carry the standard
       quality suffix (_4K/_1080P60/...) so the frontend can group & default-select.
    3. resilience: full client rotation runs inside yt.vid_info already; on top of
       that we do a second rotation round with a fresh curl session + backoff, and
       only then fall back to a real browser (headless Chromium) extraction.
    4. honesty: googlevideo hard-gates adaptive (>=1080P) streams behind GVS
       poToken (2024+) — they 403 without a valid session-bound token, and the
       old JS-decipher machinery (fmt_streams/Cipher) is dead on current player
       builds (the sig/nsig function patterns no longer match). Every candidate
       stream is therefore probed with a ranged GET and gated (403) qualities are
       dropped — the user can only select qualities that really download. When
       the poToken infrastructure starts yielding valid tokens (or the gate is
       relaxed), the higher qualities reappear automatically.
'''
class YouTubeVideoClient(BaseVideoClient):
    source = 'YouTubeVideoClient'
    # VISIONOS goes first: it is the only client whose adaptive (>=1080P) urls
    # are NOT gated behind GVS poToken, so it yields the full quality ladder.
    # ANDROID / the rest are kept as fallbacks (they only survive as
    # progressive 360P these days, but they cost nothing to try).
    PRIMARY_CLIENT = 'VISIONOS'
    FALLBACK_CLIENTS = ['ANDROID', 'ANDROID_VR', 'ANDROID_MUSIC', 'ANDROID_CREATOR', 'ANDROID_TESTSUITE', 'ANDROID_PRODUCER', 'ANDROID_KIDS', 'WEB_EMBED', 'TV', 'IOS']
    RETRY_ROUNDS = 2
    RETRY_BACKOFF_SECS = 3

    def __init__(self, **kwargs):
        super(YouTubeVideoClient, self).__init__(**kwargs)
        self.default_parse_headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"}
        # googlevideo (2024+) requires a Range header on stream requests — a
        # plain GET returns 403 even for non-gated streams. An open-ended range
        # (`bytes=0-`) serves the full body with 206 and is accepted on every
        # playable stream, so it is baked into the download headers.
        self.default_download_headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36", "Range": "bytes=0-"}
        self.default_headers = self.default_parse_headers
        self._initsession()

    '''_extractvid: v= param, then youtu.be/<id>, /shorts/<id>, /embed/<id>, /live/<id>'''
    @staticmethod
    def _extractvid(url: str) -> str:
        parsed = urlparse(url)
        vid_list = parse_qs(parsed.query, keep_blank_values=True).get('v')
        if vid_list and vid_list[0]:
            return vid_list[0]
        parts = [p for p in parsed.path.strip('/').split('/') if p]
        if parts and re.match(r'^[\w-]{8,15}$', parts[-1]):
            return parts[-1]
        return ''

    '''_qualitylabel: map (height, fps) to the frontend's standard quality suffix'''
    @staticmethod
    def _qualitylabel(height: int, fps: int = 0) -> str:
        if height >= 4320: return '8K'
        if height >= 2160: return '4K'
        if height >= 1440: return '1080P+'
        if height >= 1080: return '1080P60' if fps > 30 else '1080P'
        if height >= 720: return '720P60' if fps > 30 else '720P'
        if height >= 480: return '480P'
        if height >= 360: return '360P'
        if height >= 240: return '240P'
        return '144P'

    '''_probeurl: ranged GET — is this stream actually downloadable?

    googlevideo answers 403 (text/plain) for streams gated behind GVS poToken;
    playable streams answer 206/200 with a real media content-type. The probe
    uses the SAME open-ended range the downloader will send, so its verdict
    matches real download behavior. One retry guards against transient 403
    bursts (high request velocity).'''
    @staticmethod
    def _probeurl(u: str) -> bool:
        import requests
        for attempt in range(2):
            try:
                with requests.get(u, headers={'Range': 'bytes=0-'}, stream=True, timeout=15, allow_redirects=True) as r:
                    if r.status_code in (200, 206) and 'text/plain' not in (r.headers.get('content-type') or ''):
                        return True
                    return False
            except Exception:
                pass
            if attempt == 0: time.sleep(1)
        return False

    '''_newyt: YouTube instance with the widened client rotation pool'''
    def _newyt(self, vid: str) -> YouTube:
        yt = YouTube(video_id=vid, client=self.PRIMARY_CLIENT)
        yt.fallback_clients = list(self.FALLBACK_CLIENTS)
        return yt

    '''_innertube: run the client rotation, with a fresh-session retry round on failure.

    Returns raw_data. When every client is blocked, raw_data is the last blocked
    response (for diagnostics).'''
    def _innertube(self, vid: str, diag_steps: list) -> dict:
        raw_data = {}
        for rnd in range(self.RETRY_ROUNDS):
            if rnd > 0:
                with suppress(Exception): RequestWrapper.reset_curl_session()
                time.sleep(self.RETRY_BACKOFF_SECS * rnd)
                self.logger_handle.info(f'[DIAG youtube] innertube retry round {rnd + 1} (fresh session)', disable_print=self.disable_print)
            yt = self._newyt(vid)
            raw_data = yt.vid_info
            ps = raw_data.get('playabilityStatus', {})
            status = ps.get('status', 'ERROR')
            reason = str(ps.get('reason', ''))[:40]
            sd = raw_data.get('streamingData', {}) or {}
            has_streams = bool(sd.get('formats') or sd.get('adaptiveFormats'))
            diag_steps.append(f'Innertube[{yt.client}]={status}({reason}) streams={has_streams}')
            self.logger_handle.info(f'[DIAG youtube] {diag_steps[-1]}', disable_print=self.disable_print)
            if status == 'OK' and has_streams:
                break
        return raw_data

    '''_fetch_via_browser: real-Chromium fallback; reads ytInitialPlayerResponse.

    Per-source YouTube cookies (from the login feature) are injected so a
    logged-in session avoids the "confirm you're not a bot" wall entirely.'''
    def _fetch_via_browser(self, url: str, request_overrides: dict) -> dict:
        try:
            from vd.modules.utils.chromium import DrissionPageUtils
        except Exception as err:
            self.logger_handle.error(f'[DIAG youtube] browser fallback import failed: {err}', disable_print=self.disable_print)
            return {}
        self.logger_handle.info(f'[DIAG youtube] browser fetch START url={url}', disable_print=self.disable_print)
        page = None
        try:
            browser_path = DrissionPageUtils.findsystembrowser()
            if not browser_path:
                self.logger_handle.warning('[DIAG youtube] no system Chrome/Edge found; skipping browser fallback', disable_print=self.disable_print)
                return {}
            page = DrissionPageUtils.initsmartbrowser(
                headless=True,
                requests_cookies=(request_overrides or {}).get('cookies') or None,
                requests_cookies_domain='.youtube.com',
                requests_proxies=self._autosetproxies(),
                browser_path=browser_path,
                allow_download=False,
            )
            page.get(url)
            time.sleep(5)
            raw = page.run_js('return window.ytInitialPlayerResponse ? JSON.stringify(window.ytInitialPlayerResponse) : "";')
            if raw and isinstance(raw, str):
                data = json.loads(raw)
                self.logger_handle.info(f'[DIAG youtube] browser extract playability={data.get("playabilityStatus", {}).get("status")}', disable_print=self.disable_print)
                return data
            html = page.html
            match = re.search(r'ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;', html, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                self.logger_handle.info(f'[DIAG youtube] browser HTML extract playability={data.get("playabilityStatus", {}).get("status")}', disable_print=self.disable_print)
                return data
            self.logger_handle.warning('[DIAG youtube] no ytInitialPlayerResponse found in browser page', disable_print=self.disable_print)
        except Exception as err:
            self.logger_handle.error(f'[DIAG youtube] browser fetch failed: {err}', disable_print=self.disable_print)
        finally:
            if page:
                with suppress(Exception): DrissionPageUtils.quitpage(page)
        return {}

    '''_pickbestaudio: highest-bitrate audio, preferring the default/original
    track and an mp4 (m4a) container for clean ffmpeg merging.'''
    @staticmethod
    def _pickbestaudio(astreams: list) -> dict | None:
        if not astreams: return None
        def rank(f):
            mime = f.get('mimeType', '')
            is_mp4 = 1 if 'mp4' in mime else 0
            is_default = 1 if 'original' in str((f.get('audioTrack') or {}).get('displayName', '')) else 0
            return (is_default, is_mp4, int(f.get('bitrate', 0) or 0))
        return sorted(astreams, key=rank, reverse=True)[0]

    '''_builditems: one VideoInfo per distinct quality label, best first.

    Built directly from the raw streamingData dicts (no fmt_streams/Cipher —
    that machinery is dead on current player builds and ANDROID urls are plain).
    Progressive (muxed) streams win label collisions at <=720P (single request,
    no merge); adaptive video+audio pairs cover >=1080P via ffmpeg merge.
    The audio fields MUST always be set together with audio_download_url —
    an empty audio_save_path crashes the merge downloader (see bilibili.py).
    Every candidate is probed with a ranged GET; gated (403) streams are dropped
    so the user can only select qualities that really download.'''
    def _builditems(self, raw_data: dict, null_backup_title: str, vid: str) -> list:
        sd = raw_data.get('streamingData', {}) or {}
        video_title = legalizestring(raw_data.get('videoDetails', {}).get('title') or '', replace_null_string=null_backup_title).removesuffix('.')
        cover_url = safeextractfromdict(raw_data, ['videoDetails', 'thumbnail', 'thumbnails', -1, 'url'], None)
        prog = sorted([f for f in (sd.get('formats') or []) if f.get('url') and 'video' in f.get('mimeType', '')], key=lambda f: (int(f.get('height', 0) or 0), int(f.get('bitrate', 0) or 0)), reverse=True)
        vadap = sorted([f for f in (sd.get('adaptiveFormats') or []) if f.get('url') and 'video' in f.get('mimeType', '')], key=lambda f: (int(f.get('height', 0) or 0), int(f.get('bitrate', 0) or 0)), reverse=True)
        aaud = [f for f in (sd.get('adaptiveFormats') or []) if f.get('url') and 'audio' in f.get('mimeType', '')]
        audio = self._pickbestaudio(aaud)
        # plan selection: progressive first (stable, muxed), adaptive only for
        # labels progressive cannot reach (>=1080P typically)
        plans, seen = [], set()
        for f in prog:
            h, fps = int(f.get('height', 0) or 0), int(f.get('fps', 0) or 0)
            if h < 360: continue
            label = self._qualitylabel(h, fps)
            if label not in seen: seen.add(label); plans.append((label, f, None))
        if audio is not None:
            # VISIONOS (and most TV-like clients) serve adaptive-only manifests,
            # so when there is no progressive plan at all the adaptive ladder
            # must cover the low qualities too, otherwise we would only ever
            # offer 360P.
            min_adaptive_height = 1080 if plans else 360
            for f in vadap:
                h, fps = int(f.get('height', 0) or 0), int(f.get('fps', 0) or 0)
                if h < min_adaptive_height: continue
                label = self._qualitylabel(h, fps)
                if label not in seen: seen.add(label); plans.append((label, f, audio))
        # last resort: some very old uploads top out at 240p (or lower). Never
        # leave the user with an empty result — offer the best stream we have.
        if not plans:
            for f in vadap:
                h, fps = int(f.get('height', 0) or 0), int(f.get('fps', 0) or 0)
                plans.append((self._qualitylabel(h, fps), f, audio)); break
            if not plans and prog:
                f = prog[0]
                h, fps = int(f.get('height', 0) or 0), int(f.get('fps', 0) or 0)
                plans.append((self._qualitylabel(h, fps), f, None))
        plans.sort(key=lambda p: int(p[1].get('height', 0) or 0), reverse=True)
        # probe: drop gated (403) streams so every offered quality really downloads
        probed = []
        for label, vf, af in plans:
            if self._probeurl(vf['url']) and (af is None or self._probeurl(af['url'])):
                probed.append((label, vf, af))
            else:
                self.logger_handle.warning(f'[DIAG youtube] quality {label} gated by server (403); dropped', disable_print=self.disable_print)
        # emit
        items = []
        for label, vf, af in probed:
            _t = f'{video_title}_{label}'
            _vpi = VideoInfo(source=self.source)
            _vpi.update(dict(
                raw_data=raw_data, download_url=vf['url'], title=_t, quality=label,
                save_path=os.path.join(self.work_dir, self.source, f'{_t}.mp4'), ext='mp4',
                guess_video_ext_result=dict(ext='mp4', guessed=True),
                identifier=f'{vid}-{label}', cover_url=cover_url,
                default_download_headers=self.default_download_headers,
                default_download_cookies=self.default_download_cookies,
            ))
            if af is not None:
                _vpi.update(dict(
                    audio_download_url=af['url'],
                    audio_save_path=os.path.join(self.work_dir, self.source, f'{_t}.audio.m4a'),
                    audio_ext='m4a',
                    default_audio_download_headers=self.default_download_headers,
                    default_audio_download_cookies=self.default_download_cookies,
                ))
            items.append(_vpi)
        return items

    '''parsefromurl'''
    @useparseheaderscookies
    def parsefromurl(self, url: str, request_overrides: dict = None) -> list[VideoInfo]:
        # prepare
        if not self.belongto(url=url): return []
        request_overrides, video_info, null_backup_title = request_overrides or {}, VideoInfo(source=self.source), yieldtimerelatedtitle(self.source)
        vid = self._extractvid(url)
        if not vid:
            video_info.update(dict(err_msg=f'{self.source}.parsefromurl >>> {url} (Error: could not extract video id)'))
            return [video_info]
        diag_steps = []
        try:
            # step 1: innertube rotation (client pool + fresh-session retry rounds)
            raw_data = self._innertube(vid, diag_steps)
            ps = raw_data.get('playabilityStatus', {})
            status, sd = ps.get('status', 'ERROR'), raw_data.get('streamingData', {}) or {}
            ok = status == 'OK' and bool(sd.get('formats') or sd.get('adaptiveFormats'))
            # step 2: real-browser fallback (uses login cookies when available)
            if not ok:
                self.logger_handle.info('[DIAG youtube] innertube blocked; trying browser fallback', disable_print=self.disable_print)
                raw_data = self._fetch_via_browser(url, request_overrides)
                ps = raw_data.get('playabilityStatus', {})
                status = ps.get('status', 'EMPTY')
                sd = raw_data.get('streamingData', {}) or {}
                ok = status == 'OK' and bool(sd.get('formats') or sd.get('adaptiveFormats'))
                diag_steps.append(f'Browser={status} streams={ok}')
                self.logger_handle.info(f'[DIAG youtube] {diag_steps[-1]}', disable_print=self.disable_print)
            if not ok:
                raise RuntimeError(f'YouTube blocked: {" | ".join(diag_steps)}. Enable proxy in Settings or retry later.')
            # step 3: enumerate quality items (raw streamingData, probe-filtered)
            items = self._builditems(raw_data, null_backup_title, vid)
            if not items:
                raise RuntimeError(f'No downloadable stream found ({" | ".join(diag_steps)})')
            self.logger_handle.info(f'[DIAG youtube] parsed {len(items)} quality items: {[getattr(i, "quality", "") for i in items]}', disable_print=self.disable_print)
            return items
        except Exception as err:
            video_info.update(dict(err_msg=(err_msg := f'{self.source}.parsefromurl >>> {url} (Error: {err})')))
            self.logger_handle.error(err_msg, disable_print=self.disable_print)
        # return
        return [video_info]

    '''belongto'''
    @staticmethod
    def belongto(url: str, valid_domains: list[str] | set[str] = None):
        valid_domains = set(valid_domains or []) | {"youtube.com", "youtu.be"}
        return BaseVideoClient.belongto(url, valid_domains)
