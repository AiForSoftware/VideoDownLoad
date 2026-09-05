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

try:
    import yt_dlp
except Exception:
    yt_dlp = None


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
        # Per-video cache of fresh, n-decrypted googlevideo urls resolved by yt-dlp.
        # Populated lazily at download time (one yt-dlp call per video, reused for
        # its video + audio streams) so the IP has cooled down since the parse-phase
        # requests and the resolution is not starved by YouTube rate-limiting.
        self._yt_urls_cache = {}
        # User-configured proxy url (extracted from request_overrides['proxies']
        # at parse time). Used for the yt-dlp extraction calls so resolution goes
        # through the same, unblocked egress as the rest of the pipeline.
        self._ytdlp_proxy = ''
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

    '''_quickprobe: ONE short ranged GET (<=6s) to confirm a single *selected*
    stream is reachable (not gated by a 403). Replaces the old parse-phase probe
    that fired once per every candidate quality.'''
    @staticmethod
    def _quickprobe(u: str, timeout: int = 6) -> bool:
        import requests
        proxies = {'http': RequestWrapper._proxy_url, 'https': RequestWrapper._proxy_url} if RequestWrapper._proxy_url else None
        try:
            with requests.get(u, headers={'Range': 'bytes=0-'}, stream=True, timeout=timeout, allow_redirects=True, proxies=proxies) as r:
                if r.status_code in (200, 206) and 'text/plain' not in (r.headers.get('content-type') or ''):
                    return True
        except Exception:
            pass
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
                # prefer the proxy configured in Settings over the freeproxy
                # auto-fetch (which is disabled by default and returns {}).
                requests_proxies=(request_overrides or {}).get('proxies') or self._autosetproxies(),
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

    '''_extract_captions: pull subtitle tracks from the player response and expose
    them as `{lang, url, ext, headers, cookies}` descriptors that the base engine
    already knows how to download and mux (see BaseVideoClient._mux_subtitles_if_any).

    YouTube serves captions via the `timedtext` API in TTML by default; we force
    `fmt=vtt` so ffmpeg can mux them as WebVTT. The auto-generated ("asr") track is
    kept as a fallback but de-duplicated against the human-authored track for the
    same language so the resulting file is not cluttered with two "en" streams.'''
    @staticmethod
    def _extract_captions(raw_data: dict, headers: dict, cookies: dict) -> list:
        caps = (raw_data.get('captions') or {}).get('playerCaptionsTracklistRenderer') or {}
        tracks = caps.get('captionTracks') or []
        if not tracks:
            return []
        sub_headers = dict(headers or {})
        sub_headers.setdefault('Referer', 'https://www.youtube.com/')
        out, seen = [], set()
        for t in tracks:
            u = t.get('baseUrl')
            if not u:
                continue
            lang = str(t.get('languageCode') or 'und')
            kind = str(t.get('kind') or '')
            # Force WebVTT output from the timedtext endpoint.
            u2 = re.sub(r'[?&]fmt=[^&]+', '', u)
            sep = '&' if '?' in u2 else '?'
            url = u2 + sep + 'fmt=vtt'
            # Keep the human-authored track; skip a duplicate asr track when a
            # standard one for the same language already exists.
            is_asr = kind == 'asr'
            if is_asr and lang in seen:
                continue
            seen.add(lang)
            out.append({
                'lang': lang,
                'url': url,
                'ext': 'vtt',
                'headers': sub_headers,
                'cookies': dict(cookies or {}),
            })
        return out

    '''_builditems: one VideoInfo per distinct quality label, best first.

    Built from the raw streamingData dicts. The googlevideo urls in those dicts carry
    an encrypted `n` throttle param that YouTube uses to throttle downloads ("fast then
    slow"); _apply_ytdlp_urls rewrites them with yt-dlp-resolved (n-decrypted) urls right
    before this runs, so every quality downloads at full speed.
    Progressive (muxed) streams win label collisions at <=720P (single request,
    no merge); adaptive video+audio pairs cover >=1080P via ffmpeg merge.
    The audio fields MUST always be set together with audio_download_url —
    an empty audio_save_path crashes the merge downloader (see bilibili.py).
    Every candidate is probed with a ranged GET; gated (403) streams are dropped
    so the user can only select qualities that really download.'''
    def _resolve_via_ytdlp(self, vid: str) -> dict:
        '''Use yt-dlp (actively maintained) to resolve the real, n-decrypted
        googlevideo urls for this video. Returns {itag_str: url}.

        Retries a few times with backoff: YouTube intermittently rate-limits the
        extraction (the very same throttle that later hits the actual downloads),
        and the resolution must not silently fall back to raw, throttled urls.'''
        if yt_dlp is None:
            return {}
        last_err = None
        for attempt in range(3):
            try:
                with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True,
                                       'skip_download': True, 'noplaylist': True,
                                       **({'proxy': self._ytdlp_proxy} if self._ytdlp_proxy else {})}) as ydl:
                    info = ydl.extract_info(f'https://www.youtube.com/watch?v={vid}', download=False)
                mapping = {}
                for f in info.get('formats', []):
                    u = f.get('url')
                    # yt-dlp stores the itag in `format_id` (string, e.g. "137"); the
                    # `itag` field is None in recent versions. The app's raw_data uses
                    # integer itags, so normalise to str for matching.
                    it = f.get('format_id') or f.get('itag')
                    if u and it is not None:
                        mapping[str(it)] = u
                # Some clients only expose audio/video under requested_formats.
                for f in info.get('requested_formats', []):
                    u = f.get('url')
                    it = f.get('format_id') or f.get('itag')
                    if u and it is not None:
                        mapping.setdefault(str(it), u)
                self.logger_handle.info(f'[DIAG youtube] yt-dlp resolved {len(mapping)} stream urls (attempt {attempt + 1})', disable_print=self.disable_print)
                return mapping
            except Exception as e:
                last_err = e
                self.logger_handle.warning(f'[DIAG youtube] yt-dlp resolve attempt {attempt + 1} failed: {e}', disable_print=self.disable_print)
                if attempt < 2:
                    time.sleep(2 + attempt * 2)
        return {}

    def _apply_ytdlp_urls(self, raw_data: dict, vid: str) -> set:
        '''Replace the (possibly n-encrypted) googlevideo urls in raw_data with the
        yt-dlp-resolved ones, keyed by itag. This is what actually defeats the
        "fast then slow" YouTube throttle.

        Returns the set of itags (str) that are safe to download: either their url
        never carried an encrypted `n` (plain ANDROID/VISIONOS urls), or yt-dlp
        successfully rewrote it.

        NOTE: yt-dlp's own urls also contain an `n=` parameter (the *decrypted*
        nsig), so "url still has n=" can NOT be used to detect failure — success
        is tracked by the rewrite itself.'''
        sd = raw_data.get('streamingData', {}) or {}
        entries = [(f, f.get('itag'), f.get('url')) for key in ('formats', 'adaptiveFormats') for f in sd.get(key, [])]
        trusted = {str(it) for _, it, u in entries if it is not None and 'n=' not in (u or '')}
        todo = [(f, it) for f, it, u in entries if 'n=' in (u or '')]
        if not todo:
            return trusted
        # A single yt-dlp call frequently returns only part of the format list,
        # which leaves some streams (most visibly the best audio) still carrying
        # the encrypted n parameter and therefore throttled to ~0 bytes/s. Run a
        # second round when the first one did not cover everything.
        rewritten = set()
        for attempt in range(3):
            mapping = self._resolve_via_ytdlp(vid)
            if not mapping:
                break
            for f, it in todo:
                if it is not None and id(f) not in rewritten and str(it) in mapping:
                    f['url'] = mapping[str(it)]
                    rewritten.add(id(f)); trusted.add(str(it))
            if len(rewritten) == len(todo):
                break
            if attempt == 0: time.sleep(1)
        unresolved = [(f, it) for f, it in todo if id(f) not in rewritten]
        if unresolved:
            itags = sorted({str(it) for _, it in unresolved})
            self.logger_handle.warning(f'[DIAG youtube] {len(unresolved)} throttled stream(s) not resolved by yt-dlp (itags: {itags[:12]})', disable_print=self.disable_print)
        return trusted

    '''_fetchrawdata_via_ytdlp: last-resort extraction via yt-dlp.

    yt-dlp ships continuously-updated client rotation and bot-wall workarounds
    (web_safari / tv_embedded / android_sdkless, visitor-data reuse, ...), so it
    often still succeeds when every raw innertube client AND the browser
    fallback are blocked. The result is shaped exactly like an innertube
    player response (videoDetails / streamingData / captions /
    playabilityStatus) so _builditems can consume it unchanged. The googlevideo
    urls yt-dlp returns are already n-decrypted.'''
    def _fetchrawdata_via_ytdlp(self, vid: str, diag_steps: list) -> dict:
        if yt_dlp is None:
            return {}
        for attempt in range(2):
            try:
                opts = dict(quiet=True, no_warnings=True, skip_download=True, noplaylist=True)
                if self._ytdlp_proxy: opts['proxy'] = self._ytdlp_proxy
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(f'https://www.youtube.com/watch?v={vid}', download=False)
            except Exception as e:
                diag_steps.append(f'yt-dlp=ERROR({str(e)[:40]})')
                self.logger_handle.warning(f'[DIAG youtube] yt-dlp fallback attempt {attempt + 1} failed: {e}', disable_print=self.disable_print)
                if attempt == 0: time.sleep(2)
                continue

            def buildentry(f):
                vcodec, acodec = (f.get('vcodec') or 'none'), (f.get('acodec') or 'none')
                ext = f.get('ext') or 'mp4'
                it = f.get('format_id')
                try: it = int(it)
                except (TypeError, ValueError): pass
                entry = dict(itag=it, url=f.get('url'), ext=ext, bitrate=int((f.get('tbr') or 0) * 1000))
                if vcodec != 'none':
                    codecs = f'{vcodec}, {acodec}' if acodec != 'none' else vcodec
                    entry.update(mimeType=f'video/{ext}; codecs="{codecs}"', height=int(f.get('height') or 0),
                                 width=int(f.get('width') or 0), fps=int(f.get('fps') or 0))
                else:
                    entry.update(mimeType=f'audio/{ext}; codecs="{acodec}"', audioSampleRate=int(f.get('asr') or 0) or None)
                return entry
            all_formats = info.get('formats', []) or []
            progressive = [buildentry(f) for f in all_formats if f.get('url') and (f.get('vcodec') or 'none') != 'none' and (f.get('acodec') or 'none') != 'none']
            adaptive = [buildentry(f) for f in all_formats if f.get('url') and ((f.get('vcodec') or 'none') == 'none' or (f.get('acodec') or 'none') == 'none')]
            # captionTracks in the same shape _extract_captions expects; prefer
            # human-authored subtitles over auto-generated ones.
            subs, seen_lang = [], set()
            for lang, tracks in list((info.get('subtitles') or {}).items()) + list((info.get('automatic_captions') or {}).items()):
                if lang in seen_lang or not tracks: continue
                seen_lang.add(lang)
                u = tracks[-1].get('url')
                kind = '' if tracks is (info.get('subtitles') or {}).get(lang) else 'asr'
                if u: subs.append(dict(languageCode=lang, baseUrl=u, kind=kind))
            thumbnails = info.get('thumbnails') or []
            raw = dict(
                playabilityStatus=dict(status='OK'),
                videoDetails=dict(title=info.get('title') or '', videoId=vid,
                                  thumbnail=dict(thumbnails=thumbnails), lengthSeconds=int(info.get('duration') or 0)),
                streamingData=dict(formats=progressive, adaptiveFormats=adaptive),
                captions=dict(playerCaptionsTracklistRenderer=dict(captionTracks=subs)) if subs else {},
            )
            self.logger_handle.info(f'[DIAG youtube] yt-dlp fallback resolved {len(progressive)} progressive + {len(adaptive)} adaptive streams', disable_print=self.disable_print)
            return raw
        return {}

    '''_downloadfromyoutube: re-resolve a fresh, n-decrypted googlevideo url right
    before downloading each stream.

    WHY: the parse-phase yt-dlp resolution runs AFTER the engine has already fired a
    burst of innertube/browser requests at YouTube, so by then the IP is often
    rate-limited and the yt-dlp extraction returns nothing for the audio itags —
    leaving the audio url with its raw (throttled) `n` param, which then crawls and
    times out ("audio download failed, skipping merge"). Re-resolving at download
    time, when the IP has cooled down, is what reliably defeats the throttle. One
    yt-dlp call per video is cached and reused for both its video and audio streams.'''
    def _downloadfromyoutube(self, video_info, video_info_index=0, downloaded_video_infos=None, request_overrides=None, progress=None):
        du = getattr(video_info, 'download_url', None)
        # The parse phase already rewrote every throttled url with a yt-dlp-resolved
        # (n-decrypted) url via `_apply_ytdlp_urls`, so `du.url` is normally already
        # full-speed. The reference YoutubeDownloader never re-queries the manifest at
        # download time; re-resolving unconditionally added a full yt-dlp extract_info
        # (2-5s) on EVERY download. We now only re-resolve the rare url that still
        # carries an encrypted `n=` param (a miss by the earlier pass).
        if yt_dlp is not None and du is not None and isinstance(getattr(du, 'url', ''), str) and 'n=' in du.url:
            try:
                # identifier is "{vid}-{quality}"; quality labels never contain
                # '-', so rsplit is safe even when the video id contains '-'.
                vid = str(getattr(video_info, 'identifier', '') or '').rsplit('-', 1)[0]
                if vid:
                    if vid not in self._yt_urls_cache:
                        self._yt_urls_cache[vid] = self._resolve_via_ytdlp(vid)
                    fresh = self._yt_urls_cache[vid].get(str(getattr(du, 'itag', '')))
                    if fresh:
                        du.url = fresh
                        du._filesize = 0
                        self.logger_handle.info(
                            f'[DIAG youtube] lazily refreshed throttled stream url for itag {getattr(du, "itag", "?")} (vid {vid[:8]})',
                            disable_print=self.disable_print)
            except Exception as e:
                self.logger_handle.warning(f'[DIAG youtube] stream url refresh skipped: {e}', disable_print=self.disable_print)
        # One short reachability check on the actually-selected stream replaces the old
        # parse-phase probe that fired once per every candidate quality. This keeps the
        # "don't offer dead 403s" guarantee without the O(qualities) delay.
        if du is not None and isinstance(getattr(du, 'url', ''), str) and not self._quickprobe(du.url):
            self.logger_handle.warning(
                f'[DIAG youtube] stream itag {getattr(du, "itag", "?")} unreachable (403); skipping download',
                disable_print=self.disable_print)
            return downloaded_video_infos
        return super(YouTubeVideoClient, self)._downloadfromyoutube(
            video_info, video_info_index, downloaded_video_infos, request_overrides, progress)

    def _builditems(self, raw_data: dict, null_backup_title: str, vid: str, trusted_itags: set | None = None) -> list:
        sd = raw_data.get('streamingData', {}) or {}
        # Subtitle tracks (if any) are shared across every quality of the same
        # video, so resolve them once and attach the same list to each VideoInfo.
        subs = self._extract_captions(raw_data, self.default_download_headers, self.default_download_cookies)
        video_title = legalizestring(raw_data.get('videoDetails', {}).get('title') or '', replace_null_string=null_backup_title).removesuffix('.')
        cover_url = safeextractfromdict(raw_data, ['videoDetails', 'thumbnail', 'thumbnails', -1, 'url'], None)
        prog = sorted([f for f in (sd.get('formats') or []) if f.get('url') and 'video' in f.get('mimeType', '')], key=lambda f: (int(f.get('height', 0) or 0), int(f.get('bitrate', 0) or 0)), reverse=True)
        vadap = sorted([f for f in (sd.get('adaptiveFormats') or []) if f.get('url') and 'video' in f.get('mimeType', '')], key=lambda f: (int(f.get('height', 0) or 0), int(f.get('bitrate', 0) or 0)), reverse=True)
        aaud = [f for f in (sd.get('adaptiveFormats') or []) if f.get('url') and 'audio' in f.get('mimeType', '')]
        # Prefer audio streams whose url yt-dlp resolved (or that never needed
        # it). A slightly lower-bitrate audio that really downloads is better
        # than the "best" audio that hangs on the encrypted-n throttle.
        if trusted_itags:
            aaud_safe = [f for f in aaud if str(f.get('itag')) in trusted_itags]
            audio = self._pickbestaudio(aaud_safe if aaud_safe else aaud)
        else:
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
        # NOTE: No per-quality ranged-GET probe here anymore. Each probe used to
        # cost up to ~15s x2; with ~10 qualities that made "paste URL -> results"
        # take 30s+. The reference YoutubeDownloader trusts the resolved manifest
        # and only discovers a 403 at download time, so we now offer every resolved
        # quality and run ONE short reachability check in `_downloadfromyoutube`
        # (right before the actual download of that single stream).
        probed = plans
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
                subtitles=subs,
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
        # Ensure login cookies (if any) reach the real-browser fallback below. The
        # engine passes them via request_overrides['cookies'], but fall back to the
        # client's default cookies so the fallback never runs cookie-less.
        if not request_overrides.get('cookies') and getattr(self, 'default_cookies', None):
            request_overrides = {**request_overrides, 'cookies': self.default_cookies}
        vid = self._extractvid(url)
        if not vid:
            video_info.update(dict(err_msg=f'{self.source}.parsefromurl >>> {url} (Error: could not extract video id)'))
            return [video_info]
        # Route EVERY parse-phase channel through the user-configured proxy (if
        # any): innertube requests (RequestWrapper), the browser fallback and
        # yt-dlp. Previously the proxy only reached the download phase, so a
        # flagged IP kept failing at parse time even with a working proxy
        # configured in Settings.
        proxy_url = ''
        with suppress(Exception):
            _proxies = request_overrides.get('proxies') or {}
            proxy_url = _proxies.get('https') or _proxies.get('http') or ''
        RequestWrapper.set_proxy(proxy_url)
        self._ytdlp_proxy = proxy_url
        diag_steps = []
        from_ytdlp = False
        has_login = bool(request_overrides.get('cookies'))
        try:
            if has_login:
                # With login cookies available, the bot-walled innertube clients are
                # slow and unreliable; go straight to the real-browser fallback, which
                # carries the session and reliably returns streamingData.
                self.logger_handle.info('[DIAG youtube] login cookies present; using browser fallback directly', disable_print=self.disable_print)
                raw_data = self._fetch_via_browser(url, request_overrides)
                ps = raw_data.get('playabilityStatus', {})
                status, sd = ps.get('status', 'EMPTY'), raw_data.get('streamingData', {}) or {}
                ok = status == 'OK' and bool(sd.get('formats') or sd.get('adaptiveFormats'))
                diag_steps.append(f'Browser={status} streams={ok}')
                self.logger_handle.info(f'[DIAG youtube] {diag_steps[-1]}', disable_print=self.disable_print)
            else:
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
                # step 2.7: last-resort yt-dlp extraction — its client rotation
                # and bot-wall workarounds are updated far more often than the
                # raw innertube clients above, so it frequently still succeeds
                # when everything else is blocked.
                self.logger_handle.info('[DIAG youtube] innertube/browser blocked; trying yt-dlp fallback', disable_print=self.disable_print)
                raw_data = self._fetchrawdata_via_ytdlp(vid, diag_steps)
                ps = raw_data.get('playabilityStatus', {})
                status = ps.get('status', 'EMPTY')
                sd = raw_data.get('streamingData', {}) or {}
                ok = status == 'OK' and bool(sd.get('formats') or sd.get('adaptiveFormats'))
                from_ytdlp = ok
                diag_steps.append(f'yt-dlp={status} streams={ok}')
                self.logger_handle.info(f'[DIAG youtube] {diag_steps[-1]}', disable_print=self.disable_print)
            if not ok:
                raise RuntimeError(f'YouTube blocked: {" | ".join(diag_steps)}. Enable proxy in Settings or retry later.')
            # step 2.5: rewrite googlevideo urls with yt-dlp-resolved (n-decrypted) urls
            if from_ytdlp:
                # the fallback already returns n-decrypted urls; just trust them all
                trusted_itags = {str(f.get('itag')) for key in ('formats', 'adaptiveFormats')
                                 for f in (raw_data.get('streamingData', {}) or {}).get(key, []) if f.get('itag') is not None}
            else:
                trusted_itags = self._apply_ytdlp_urls(raw_data, vid)
            # step 3: enumerate quality items (raw streamingData, probe-filtered)
            items = self._builditems(raw_data, null_backup_title, vid, trusted_itags=trusted_itags)
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
