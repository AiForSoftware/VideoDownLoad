'''
Function:
    Implementation of BilibiliVideoClient
'''
import os
import re
import copy
import time
import hashlib
from contextlib import suppress
from .base import BaseVideoClient
from urllib.parse import urlparse, parse_qs, urlencode
from ..utils.domains import BILIBILI_SUFFIXES
from ..utils import legalizestring, resp2json, useparseheaderscookies, yieldtimerelatedtitle, safeextractfromdict, taskprogress, FileTypeSniffer, VideoInfo


_BILI_QN_LABEL = {127: '8K', 126: '1080P+', 125: '1080P', 120: '4K', 116: '1080P60', 112: '1080P+', 80: '1080P', 74: '720P60', 64: '720P', 32: '480P', 16: '360P', 15: '360P'}


# B站 WBI 签名：playurl 等接口对已登录用户需要 wbi 签名（w_rid/wts）才会返回
# 完整（含高码率）的 dash 流；缺签名会被降级或 -404。wbi_img 来自 nav 接口，
# 未登录也能拿到，因此签名对登录/未登录都做，失败则回退 durl 逻辑。
_BILI_WBI_ENC_TABLE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 57, 22, 45, 44, 36, 13, 21, 11, 56, 34, 54, 25, 52, 6, 19, 59, 20, 51, 60, 4, 30, 22, 62, 63, 57, 52, 37, 47, 34, 43, 28, 21, 8, 41, 19]
_BILI_WBI_KEYS_CACHE = {'keys': None}


def _bili_get_wbi_keys(client):
    if _BILI_WBI_KEYS_CACHE['keys']:
        return _BILI_WBI_KEYS_CACHE['keys']
    try:
        resp = client.get('https://api.bilibili.com/x/web-interface/nav')
        data = (resp.json().get('data', {}) or {}) if hasattr(resp, 'json') else {}
        img = (data.get('wbi_img') or {}).get('img_url', '')
        sub = (data.get('wbi_img') or {}).get('sub_url', '')
        img_key = img.rsplit('/', 1)[-1].split('.')[0]
        sub_key = sub.rsplit('/', 1)[-1].split('.')[0]
        _BILI_WBI_KEYS_CACHE['keys'] = (img_key, sub_key)
    except Exception:
        _BILI_WBI_KEYS_CACHE['keys'] = None
    return _BILI_WBI_KEYS_CACHE['keys']


def _bili_sign_wbi(params: dict) -> dict:
    keys = _BILI_WBI_KEYS_CACHE['keys']
    if not keys or not all(keys):
        return params
    img_key, sub_key = keys
    mixin = ''.join((img_key + sub_key)[i] for i in _BILI_WBI_ENC_TABLE)[:32]
    params = dict(params)
    params['wts'] = int(time.time())
    items = []
    for k in sorted(params.keys()):
        if k.startswith('w_'):
            continue
        v = params[k]
        if isinstance(v, bool):
            v = '1' if v else '0'
        items.append(f'{k}={v}')
    params['w_rid'] = hashlib.md5(('&'.join(items) + mixin).encode('utf-8')).hexdigest()
    return params

'''BilibiliVideoClient'''
class BilibiliVideoClient(BaseVideoClient):
    source = 'BilibiliVideoClient'
    def __init__(self, **kwargs):
        super(BilibiliVideoClient, self).__init__(**kwargs)
        self.default_parse_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36', 'Referer': 'https://www.bilibili.com/',}
        self.default_download_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36', 'Referer': 'https://www.bilibili.com/',}
        self.default_headers = self.default_parse_headers
        self._initsession()
    '''_bili_view'''
    def _bili_view(self, bvid: str = None, aid: str = None, request_overrides: dict = None):
        '''Fetch video metadata. Bilibili now 412s the unsigned x/web-interface/view
        under risk control, so we call the WBI-signed x/web-interface/wbi/view. Falls
        back to the unsigned endpoint when WBI keys are unavailable. Returns the parsed
        JSON (with .data.pages) or None.'''
        _params = {}
        if bvid:
            _params['bvid'] = bvid
        elif aid:
            _params['aid'] = str(aid)
        # 1) WBI-signed view (preferred; bypasses the 412 risk-control)
        try:
            _bili_get_wbi_keys(self)
            _signed = _bili_sign_wbi(dict(_params))
            _resp = self.get(f"https://api.bilibili.com/x/web-interface/wbi/view?{urlencode(_signed)}", **(request_overrides or {}))
            _resp.raise_for_status()
            _data = resp2json(resp=_resp)
            if isinstance(_data, dict) and isinstance(_data.get('data'), dict):
                return _data
        except Exception as _e:
            self.logger_handle.error(f'{self.source}._bili_view >>> wbi view failed ({_e}); trying unsigned', disable_print=self.disable_print)
        # 2) unsigned view (legacy fallback)
        try:
            _resp = self.get(f"https://api.bilibili.com/x/web-interface/view?{urlencode(_params)}", **(request_overrides or {}))
            _resp.raise_for_status()
            _data = resp2json(resp=_resp)
            if isinstance(_data, dict) and isinstance(_data.get('data'), dict):
                return _data
        except Exception as _e:
            self.logger_handle.error(f'{self.source}._bili_view >>> unsigned view failed ({_e})', disable_print=self.disable_print)
        return None

    '''_parsefromcommonurl'''
    def _parsefromcommonurl(self, url: str, request_overrides: dict = None):
        # prepare
        if not self.belongto(url=url): return []
        request_overrides, video_info, null_backup_title, video_infos = request_overrides or {}, VideoInfo(source=self.source), yieldtimerelatedtitle(self.source), []
        # B站 风控：playurl 需带 bili_ticket (取自登录 Cookie) 与 web_location 才会
        # 返回 dash 高清流；缺了即使已登录也会被降级到 720P。wbi 签名会自动把这两个
        # 参数纳入，因此只要放进 _params 即可。
        _cookies_bt = (request_overrides or {}).get('cookies') or {}
        def _bt_val(n):
            v = _cookies_bt.get(n)
            return v.get('value', '') if isinstance(v, dict) else (v or '')
        _bili_ticket = _bt_val('bili_ticket')
        # [DIAG] confirm whether login cookies actually reach the parser request
        self.logger_handle.debug(f'[DIAG bilibili] cookie_present={bool((request_overrides or {}).get("cookies"))} cookies_keys={list((request_overrides or {}).get("cookies", {}).keys())}', disable_print=self.disable_print)
        video_id, prefix = re.compile(r'https?://(?:www\.)?bilibili\.com/(?:video/|festival/[^/?#]+\?(?:[^#]*&)?bvid=)(?P<prefix>[aAbB][vV])(?P<id>[^/?#&]+)').match(url).group('id', 'prefix')
        # try parse
        try:
            part_id = int(part_id[0]) if (part_id := parse_qs(urlparse(url).query, keep_blank_values=True).get('p', None)) and isinstance(part_id, list) and str(part_id[0]).lstrip("+-").isdigit() else None
            raw_data = None
            # B站风控升级：未签名的 x/web-interface/view 现会返回 412，必须改用
            # WBI 签名的 x/web-interface/wbi/view（见 _bili_view）；拿不到元数据
            # 时直接抛错，由文末的 720P 浏览器安全网兜底。
            if prefix.upper() in ['BV']:
                raw_data = self._bili_view(bvid=f'BV{video_id}', request_overrides=request_overrides)
            elif prefix.upper() in ['AV']:
                raw_data = self._bili_view(aid=video_id, request_overrides=request_overrides)
            else:
                raw_data = None
            if not (isinstance(raw_data, dict) and isinstance(raw_data.get('data'), dict) and raw_data['data'].get('pages')):
                self.logger_handle.warning(f'{self.source}._parsefromcommonurl >>> view API unavailable, will rely on browser fallback', disable_print=self.disable_print)
                raise RuntimeError('bilibili view API unavailable (412/blocked)')
            if prefix.upper() in ['AV']:
                video_id = raw_data['data']['bvid']
            with taskprogress(description='Possible Multiple Videos Detected >>> Parsing One by One', total=len((extracted_video_items := raw_data['data']["pages"]))) as progress:
                for video_idx, extracted_video_item in enumerate(extracted_video_items):
                    if (part_id and video_idx + 1 != part_id) or (not isinstance(extracted_video_item, dict)): progress.advance(1); continue
                    # Quality enumeration: fetch the highest available quality
                    # (qn=80, usually 1080P) and, when the video exposes a
                    # separate qn=64 (720P) stream, also fetch that one. Each
                    # available quality becomes its own VideoInfo so the GUI
                    # can show per-quality items the user can select (the
                    # desktop frontend already supports multiple items per
                    # parse — selection is just ticking the rows).
                    # ---- 优先：用真实浏览器加载播放页抓取 playurl（绕过 wbi/bili_ticket
                    # 反爬）。由 B站 前端自己完成签名 + bili_ticket 生成，直接拿到含
                    # 4K/8K/HDR 的 dash；未登录/浏览器方案失败则回退下面 requests 逻辑。----
                    _ddata = None
                    # Always try the browser route (system Edge) so that even
                    # UN-logged-in users get 720P (durl) — B站's front-end JS
                    # generates bili_ticket and returns durl 720P without login.
                    # The previous `SESSDATA in _cookies_bt` guard silently
                    # skipped this for anon users, which is why the user saw
                    # "连 720p 都没有". On failure we still fall through to
                    # the requests-based dash/durl probes below.
                    try:
                        _ddata = self._fetch_playurl_via_browser(url, video_id, extracted_video_item['cid'], _cookies_bt, proxies=self._autosetproxies())
                    except Exception as _e:
                        self.logger_handle.error(f'{self.source}._parsefromcommonurl >>> browser probe failed ({_e})', disable_print=self.disable_print)
                    # ---- 回退：requests 直接调 playurl（wbi 签名）----
                    if _ddata is None:
                        try:
                            _bili_get_wbi_keys(self)
                            _params = {'otype': 'json', 'fnver': 0, 'fnval': 16, 'fourk': 1,
                                       'bvid': video_id, 'cid': extracted_video_item['cid'], 'platform': 'html5'}
                            if _bili_ticket:
                                _params['bili_ticket'] = _bili_ticket
                                _params['web_location'] = 'playurl'
                            _params = _bili_sign_wbi(_params)
                            _dresp = self.get(f"https://api.bilibili.com/x/player/playurl?{urlencode(_params)}", **request_overrides)
                            _dresp.raise_for_status()
                            _ddata = (resp2json(resp=_dresp) if hasattr(_dresp, 'text') else {}).get('data', {}) or {}
                        except Exception as _e:
                            self.logger_handle.error(f'{self.source}._parsefromcommonurl >>> dash probe failed ({_e})', disable_print=self.disable_print)
                            _ddata = None
                    _dash = _ddata.get('dash') if isinstance(_ddata, dict) else None
                    _vcount = len(_dash.get('video') or []) if isinstance(_dash, dict) else 0
                    self.logger_handle.info(f'[DIAG bilibili] dash_present={bool(_dash)} dash_video_streams={_vcount}', disable_print=self.disable_print)
                    if isinstance(_dash, dict):
                        _vids = _dash.get('video') or []
                        _auds = _dash.get('audio') or []
                        _dash_video_items = [v for v in _vids if isinstance(v, dict) and (v.get('baseUrl') or v.get('base_url'))]
                        _dash_audio_url = None
                        if _auds:
                            _da = max(_auds, key=lambda a: (a.get('bandwidth') or 0))
                            _dash_audio_url = _da.get('baseUrl') or _da.get('base_url')
                        if _dash_video_items:
                            _ep_base = len(video_infos) + 1
                            _total_pages = len(raw_data['data']['pages'])
                            _seen = set()
                            for _vi in sorted(_dash_video_items, key=lambda v: int(v.get('id', 0) or 0), reverse=True):
                                _qn = int(_vi.get('id', 0) or 0)
                                if _qn in _seen:
                                    continue
                                _seen.add(_qn)
                                _dl = _vi.get('baseUrl') or _vi.get('base_url')
                                _label = _BILI_QN_LABEL.get(_qn, f'qn{_qn}')
                                _bt = legalizestring(
                                    (f"EP{_ep_base}-{extracted_video_item.get('part')}" if _total_pages > 1 else safeextractfromdict(raw_data, ['data', 'title'], None)) or null_backup_title,
                                    replace_null_string=null_backup_title,
                                ).removesuffix('.')
                                _vpi_title = f"{_bt}_{_label}"
                                _vpi = copy.deepcopy(video_info)
                                _ext_info = FileTypeSniffer.getfileextensionfromurl(url=_dl, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
                                _ext = _ext_info['ext'] if _ext_info['ext'] and _ext_info['ext'] != 'NULL' else 'mp4'
                                if _ext in {'m4s'}: _ext = 'mp4'
                                _vpi.update(dict(
                                    raw_data=_ddata, download_url=_dl, title=_vpi_title, quality=_label,
                                    save_path=os.path.join(self.work_dir, self.source, f'{_vpi_title}.{_ext}'),
                                    ext=_ext, guess_video_ext_result=_ext_info,
                                    identifier=f"{video_id}-{extracted_video_item['cid']}-q{_qn}",
                                    cover_url=safeextractfromdict(extracted_video_item, ['first_frame'], None) or safeextractfromdict(raw_data, ['data', 'pic'], None),
                                ))
                                if _dash_audio_url:
                                    # audio_save_path / audio_ext MUST be set together with
                                    # audio_download_url: _downloadwithnaiveallinone builds the
                                    # audio VideoInfo from these fields, and an empty
                                    # audio_save_path makes it call touchdir(dirname('')) ->
                                    # os.makedirs('') -> "[WinError 3] 系统找不到指定的路径。: ''"
                                    _vpi.update(dict(
                                        audio_download_url=_dash_audio_url,
                                        audio_save_path=os.path.join(self.work_dir, self.source, f'{_vpi_title}.audio.m4a'),
                                        audio_ext='m4a',
                                        default_audio_download_headers=self.default_download_headers,
                                        default_audio_download_cookies=self.default_download_cookies,
                                    ))
                                video_infos.append(_vpi)
                            progress.advance(1)
                            continue
                    # ---- 浏览器已抓到 durl（未登录/风控下常见）直接复用，避免再次 requests 触发 412 ----
                    if isinstance(_ddata, dict) and isinstance(_ddata.get('durl'), list) and _ddata.get('durl'):
                        _dl = max(_ddata['durl'], key=lambda x: (x.get('size') or 0)).get('url')
                        if _dl:
                            _vpi = copy.deepcopy(video_info)
                            _ext_info = FileTypeSniffer.getfileextensionfromurl(url=_dl, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
                            _ext = _ext_info['ext'] if _ext_info['ext'] and _ext_info['ext'] != 'NULL' else 'mp4'
                            if _ext in {'m4s'}: _ext = 'mp4'
                            _bt = legalizestring((safeextractfromdict(raw_data, ['data', 'title'], None)) or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
                            _vpi.update(dict(raw_data=_ddata, download_url=_dl, title=f"{_bt}_720P(durl)", quality='720P(durl)',
                                             save_path=os.path.join(self.work_dir, self.source, f'{_bt}_720P(durl).{_ext}'),
                                             ext=_ext, guess_video_ext_result=_ext_info,
                                             identifier=f"{video_id}-{extracted_video_item['cid']}-durl",
                                             cover_url=safeextractfromdict(raw_data, ['data', 'pic'], None)))
                            video_infos.append(_vpi)
                            progress.advance(1); continue
                    # ---- 回退：durl（fnval=0，无签名）----
                    resp = None
                    with suppress(Exception):
                        _dparams = {'otype': 'json', 'fnver': 0, 'fnval': 0, 'qn': 80, 'bvid': video_id, 'cid': extracted_video_item['cid'], 'platform': 'html5'}
                        if _bili_ticket:
                            _dparams['bili_ticket'] = _bili_ticket
                            _dparams['web_location'] = 'playurl'
                        _dparams = _bili_sign_wbi(_dparams)
                        resp = self.get(f"https://api.bilibili.com/x/player/playurl?{urlencode(_dparams)}", **request_overrides)
                        resp.raise_for_status()
                    if resp is None or not hasattr(resp, 'text'):
                        progress.advance(1); continue
                    page_raw_data = resp2json(resp=resp)
                    accept_quality = [int(q) for q in page_raw_data.get('data', {}).get('accept_quality', []) or [] if str(q).isdigit()]
                    granted_quality = int(page_raw_data.get('data', {}).get('quality', 80) or 80)
                    self.logger_handle.info(f'[DIAG bilibili] durl accept_quality={accept_quality} granted={granted_quality}', disable_print=self.disable_print)
                    # Expose EVERY quality the video offers as its own VideoInfo so
                    # the desktop UI can let the user pick (e.g. 360P/480P/720P/1080P/4K).
                    # Keep a stable high→low order and de-duplicate; always include the
                    # granted (actually playable) quality as a fallback.
                    qn_requests = []
                    for qn in sorted(set(accept_quality), reverse=True):
                        if qn not in qn_requests:
                            qn_requests.append(qn)
                    if granted_quality not in qn_requests:
                        qn_requests.append(granted_quality)
                    # EP base number is the current append position before
                    # we add the new per-quality entries, so multiple
                    # qualities of the same page share the same EP number.
                    ep_base = len(video_infos) + 1
                    total_pages = len(raw_data['data']['pages'])
                    for qn in qn_requests:
                        prd = page_raw_data if qn == 80 else None
                        if prd is None:
                            r = None
                            with suppress(Exception):
                                r = self.get(
                                    f"https://api.bilibili.com/x/player/playurl?otype=json&fnver=0&fnval=0&qn={qn}&bvid={video_id}&cid={extracted_video_item['cid']}&platform=html5",
                                    **request_overrides,
                                )
                                r.raise_for_status()
                            if r is None or not hasattr(r, 'text'):
                                continue
                            prd = resp2json(resp=r)
                        prd['x/web-interface/view'] = copy.deepcopy(raw_data)
                        try:
                            dl_url = max(prd['data']['durl'], key=lambda x: x['size'])['url']
                        except Exception:
                            continue
                        label = _BILI_QN_LABEL.get(int(qn), f'qn{qn}')
                        base_title = legalizestring(
                            (f"EP{ep_base}-{extracted_video_item.get('part')}" if total_pages > 1 else safeextractfromdict(raw_data, ['data', 'title'], None)) or null_backup_title,
                            replace_null_string=null_backup_title,
                        ).removesuffix('.')
                        vpi_title = f"{base_title}_{label}"
                        vpi = copy.deepcopy(video_info)
                        ext_info = FileTypeSniffer.getfileextensionfromurl(url=dl_url, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
                        ext = ext_info['ext'] if ext_info['ext'] and ext_info['ext'] != 'NULL' else vpi['ext']
                        if ext in {'m4s'}: ext = 'mp4'
                        vpi.update(dict(
                            raw_data=prd, download_url=dl_url, title=vpi_title, quality=label,
                            save_path=os.path.join(self.work_dir, self.source, f'{vpi_title}.{ext}'),
                            ext=ext, guess_video_ext_result=ext_info,
                            identifier=f"{video_id}-{extracted_video_item['cid']}-q{qn}",
                            cover_url=safeextractfromdict(extracted_video_item, ['first_frame'], None) or safeextractfromdict(prd['x/web-interface/view'], ['data', 'pic'], None),
                        ))
                        video_infos.append(vpi)
                    progress.advance(1)
        except Exception as err:
            video_info.update(dict(err_msg=(err_msg := f'{self.source}._parsefromcommonurl >>> {url} (Error: {err})'))); video_infos.append(video_info)
            self.logger_handle.error(err_msg, disable_print=self.disable_print)
        # ---- default 720P safety net ----
        # 若上述所有方式都没解析出任何可下载结果（API 被风控 / 浏览器抓取失败等），
        # 最后再用真实浏览器加载播放页抓一次 playurl，并固定产出一条 720P 结果，
        # 保证用户至少能下载（"不能解析就默认 720p"）。
        if not any(getattr(_v, 'with_valid_download_url', False) for _v in video_infos):
            try:
                self.logger_handle.warning('[DIAG bilibili] no valid parse result; attempting browser 720P fallback', disable_print=self.disable_print)
                _fb = self._fetch_playurl_via_browser(url, video_id, 0, _cookies_bt, proxies=self._autosetproxies())
                _vpi = self._build_bili_720p(_fb, video_id, request_overrides, null_backup_title, raw_data if isinstance(raw_data, dict) else None)
                if _vpi is not None:
                    video_infos = [_vpi]
            except Exception as _e:
                self.logger_handle.error(f'{self.source}._parsefromcommonurl >>> 720p fallback failed ({_e})', disable_print=self.disable_print)
        # return
        return video_infos
    '''_fetch_playurl_via_browser'''
    def _fetch_playurl_via_browser(self, url: str, video_id: str, cid: int, cookies_dict: dict = None, proxies: dict = None):
        '''Load the Bilibili player page in a real (DrissionPage-driven) Chromium and
        let its front-end perform the WBI signing + bili_ticket generation, then
        intercept the playurl response to obtain the full dash (incl. 4K/8K/HDR). The
        stored login cookies are injected so the session is treated as the logged-in
        (premium) user. Uses an isolated temp profile, so the user's own Chrome does
        NOT need to be closed.'''
        import json
        from vd.modules.utils.chromium import DrissionPageUtils
        # diagnostic progress: info level (only real failures log as error, so the
        # desktop UI log panel never shows harmless steps in red)
        self.logger_handle.info(f'[DIAG bilibili] browser fetch START url={url} cid={cid}', disable_print=self.disable_print)
        page = None
        try:
            # DrissionPageUtils.initsmartbrowser accepts requests_cookies (dict) and
            # requests_proxies, mirroring WebMediaGrabber.buildbrowserpage — this gives
            # us the same hardened browser config (timeouts, no-imgs, proxies, etc.)
            # that the WebMediaGrabber fallback uses.
            _cookies_kv = {}
            for _k, _v in (cookies_dict or {}).items():
                _cookies_kv[_k] = _v.get('value', '') if isinstance(_v, dict) else (_v or '')
            _bp = DrissionPageUtils.findsystembrowser()
            if not _bp:
                self.logger_handle.warning('[DIAG bilibili] browser fetch SKIPPED: no system Chrome/Edge found; falling back to requests', disable_print=self.disable_print)
                return None
            page = DrissionPageUtils.initsmartbrowser(
                headless=True,
                requests_cookies=_cookies_kv or None,
                requests_cookies_domain='.bilibili.com',
                requests_proxies=proxies,
                browser_path=_bp,
                allow_download=False,
            )
            # the CDP listener is only a FALLBACK: bilibili's risk-control bootstrap
            # fires the playurl XHR at unpredictable times (and the interception
            # domain arms async), so the PRIMARY data path is deterministic — read
            # the playurl request URL from the browser's performance timeline
            # (recorded no matter what our listener does) and re-issue it from
            # inside the page, reusing the page's own cookies and WBI signature.
            try: page.listen.start('api.bilibili.com')
            except Exception: pass
            time.sleep(1)
            page.get(url)
            # B站 needs a few seconds for its front-end (bilibili-player) to bootstrap,
            # generate bili_ticket, and fire the playurl XHR.
            self.logger_handle.info('[DIAG bilibili] browser page loaded, settling 4s for JS init', disable_print=self.disable_print)
            time.sleep(4)
            try: self.logger_handle.debug(f'[DIAG bilibili] page state title={page.title!r} url={str(page.url)[:110]}', disable_print=self.disable_print)
            except Exception: pass

            def _jstext(script, *args):
                try: return page.run_js(script, *args)
                except Exception: return None

            def _accepted(_parsed):
                if not isinstance(_parsed, dict): return None
                _d = _parsed.get('data') or {}
                if isinstance(_d.get('dash'), dict) and _d['dash'].get('video'): return _d
                if _d.get('durl') is not None: return _d
                return None

            def _readplayinfo():
                try:
                    _raw = _jstext('return window.__playinfo__ ? JSON.stringify(window.__playinfo__) : "";')
                    if _raw and isinstance(_raw, str):
                        return _accepted(json.loads(_raw))
                except Exception:
                    pass
                return None

            def _refetchplayurl(target_cid: int):
                '''Find the player's playurl request on the performance timeline and
                replay it via an in-page sync XHR (same cookies + WBI signature).'''
                _entry = _jstext(
                    'var cid = String(arguments[0] || "");'
                    'var u = performance.getEntriesByType("resource").map(function(e){return e.name;})'
                    '.filter(function(n){return n.indexOf("playurl") >= 0;});'
                    'var t = "";'
                    'for (var i = u.length - 1; i >= 0; i--) {'
                    '  if (!cid || u[i].indexOf("cid=" + cid) >= 0) { t = u[i]; break; }'
                    '}'
                    'return t;', target_cid)
                if not _entry or not isinstance(_entry, str):
                    return None
                _raw = _jstext(
                    'var x = new XMLHttpRequest(); x.open("GET", arguments[0], false); x.send(null);'
                    'return x.responseText;', _entry)
                if not _raw or not isinstance(_raw, str):
                    return None
                try: return _accepted(json.loads(_raw))
                except Exception: return None

            def _navigate_to_cid(target_cid: int) -> bool:
                '''Multi-part (分P) videos: the player fetches playurl for whichever
                part the page shows. If the requested cid belongs to another part,
                navigate to its ?p= index so its playurl gets issued too.'''
                try:
                    _info = _jstext(
                        'var s = window.__INITIAL_STATE__;'
                        'if (!s || !s.videoData) return "";'
                        'var ps = s.videoData.pages || [];'
                        'var out = {cur: String(s.videoData.cid || ""), p: 0};'
                        'for (var i = 0; i < ps.length; i++) {'
                        '  if (String(ps[i].cid) === String(arguments[0])) out.p = ps[i].page || (i + 1);'
                        '}'
                        'return JSON.stringify(out);', target_cid)
                    _inf = json.loads(_info) if isinstance(_info, str) and _info else {}
                    _p = int(_inf.get('p') or 0)
                    if not _p or str(_inf.get('cur') or '') == str(target_cid):
                        return False
                    _sep = '&' if '?' in (url or '') else '?'
                    page.get(f'{url}{_sep}p={_p}')
                    time.sleep(4)
                    self.logger_handle.info(f'[DIAG bilibili] navigated to ?p={_p} for cid={target_cid}', disable_print=self.disable_print)
                    return True
                except Exception:
                    return False

            _ddata = _refetchplayurl(cid) or _readplayinfo()
            _deadline = time.time() + 60
            _navigated = False
            while _ddata is None and time.time() < _deadline:
                # listener fallback: the original XHR body may be buffered here
                try:
                    _pk = page.listen.wait(timeout=3)
                except Exception:
                    _pk = None
                if _pk:
                    try:
                        _pkurl = str(getattr(_pk, 'url', '') or '')
                        if 'playurl' in _pkurl:
                            _body = _pk.response.body
                            if isinstance(_body, bytes):
                                _body = _body.decode('utf-8', 'ignore')
                            self.logger_handle.debug(f'[DIAG bilibili] playurl packet url={_pkurl[:130]} body_head={str(_body)[:150]}', disable_print=self.disable_print)
                            _ddata = _accepted(json.loads(_body) if isinstance(_body, str) else {})
                    except Exception as _pe:
                        self.logger_handle.debug(f'[DIAG bilibili] packet parse failed: {_pe}', disable_print=self.disable_print)
                if _ddata is None:
                    _ddata = _refetchplayurl(cid)
                # multi-part: after the first ~20s without data, jump to the part
                # the requested cid belongs to so its playurl gets issued
                if _ddata is None and not _navigated and (_deadline - time.time()) < 40:
                    _navigated = _navigate_to_cid(cid)
            self.logger_handle.info(f'[DIAG bilibili] browser fetch DONE ddata={"dash" if isinstance(_ddata, dict) and _ddata.get("dash") else ("durl" if _ddata else "none")}', disable_print=self.disable_print)
            return _ddata
        except Exception as _e:
            self.logger_handle.error(f'[DIAG bilibili] browser fetch FAILED: {_e}', disable_print=self.disable_print)
            return None
        finally:
            if page:
                try: DrissionPageUtils.quitpage(page)
                except Exception: pass

    '''_build_bili_720p'''
    def _build_bili_720p(self, ddata, video_id: str, request_overrides: dict, null_backup_title: str, raw_data: dict = None):
        '''Build a SINGLE VideoInfo from a playurl response, preferring the 720P
        (qn=64) dash stream; if no dash, fall back to the durl (720P for anonymous
        users). This is the "default 720p" last-resort result when quality
        enumeration fails.'''
        if not isinstance(ddata, dict):
            return None
        video_info = VideoInfo(source=self.source)
        _dash = ddata.get('dash')
        if isinstance(_dash, dict) and _dash.get('video'):
            _vids = [v for v in (_dash.get('video') or []) if isinstance(v, dict) and (v.get('baseUrl') or v.get('base_url'))]
            if _vids:
                _vids_sorted = sorted(_vids, key=lambda v: int(v.get('id', 0) or 0), reverse=True)
                _vi = next((v for v in _vids_sorted if int(v.get('id', 0) or 0) == 64), _vids_sorted[0])
                _qn = int(_vi.get('id', 0) or 0)
                _dl = _vi.get('baseUrl') or _vi.get('base_url')
                _auds = _dash.get('audio') or []
                _audio_url = None
                if _auds:
                    _da = max(_auds, key=lambda a: (a.get('bandwidth') or 0))
                    _audio_url = _da.get('baseUrl') or _da.get('base_url')
                _label = _BILI_QN_LABEL.get(_qn, f'qn{_qn}') or '720P'
                _title = safeextractfromdict(raw_data, ['data', 'title'], None) if isinstance(raw_data, dict) else None
                _bt = legalizestring(_title or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
                _ext = self._guess_bili_ext(_dl, request_overrides)
                _vpi = copy.deepcopy(video_info)
                _vpi.update(dict(
                    raw_data=ddata, download_url=_dl, title=f"{_bt}_{_label}", quality=_label,
                    save_path=os.path.join(self.work_dir, self.source, f"{_bt}_{_label}.{_ext}"),
                    ext=_ext, guess_video_ext_result=dict(ext=_ext, guessed=True),
                    identifier=f"{video_id}-720p-q{_qn}",
                    cover_url=safeextractfromdict(raw_data, ['data', 'pic'], None) if isinstance(raw_data, dict) else None,
                ))
                if _audio_url:
                    # keep audio_save_path / audio_ext in sync (see the dash-item
                    # comment above: an empty audio_save_path crashes the merge
                    # downloader with "[WinError 3] ... : ''")
                    _vpi.update(dict(
                        audio_download_url=_audio_url,
                        audio_save_path=os.path.join(self.work_dir, self.source, f"{_bt}_{_label}.audio.m4a"),
                        audio_ext='m4a',
                        default_audio_download_headers=self.default_download_headers,
                        default_audio_download_cookies=self.default_download_cookies,
                    ))
                return _vpi
        _durl = ddata.get('durl')
        if isinstance(_durl, list) and _durl:
            _dl = max(_durl, key=lambda x: (x.get('size') or 0)).get('url')
            if _dl:
                _title = safeextractfromdict(raw_data, ['data', 'title'], None) if isinstance(raw_data, dict) else None
                _bt = legalizestring(_title or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
                _ext = self._guess_bili_ext(_dl, request_overrides)
                _vpi = copy.deepcopy(video_info)
                _vpi.update(dict(
                    raw_data=ddata, download_url=_dl, title=f"{_bt}_720P(durl)", quality='720P(durl)',
                    save_path=os.path.join(self.work_dir, self.source, f'{_bt}_720P(durl).{_ext}'),
                    ext=_ext, guess_video_ext_result=dict(ext=_ext, guessed=True),
                    identifier=f"{video_id}-720p-durl",
                    cover_url=safeextractfromdict(raw_data, ['data', 'pic'], None) if isinstance(raw_data, dict) else None,
                ))
                return _vpi
        return None

    '''_guess_bili_ext'''
    def _guess_bili_ext(self, url: str, request_overrides: dict):
        try:
            _info = FileTypeSniffer.getfileextensionfromurl(url=url, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
            _ext = _info['ext'] if _info.get('ext') and _info['ext'] != 'NULL' else 'mp4'
        except Exception:
            _ext = 'mp4'
        if _ext in {'m4s'}: _ext = 'mp4'
        return _ext

    '''_parsefrombangumiepurl'''
    def _parsefrombangumiepurl(self, url: str, request_overrides: dict = None):
        # prepare
        if not self.belongto(url=url): return []
        request_overrides, video_info, null_backup_title, video_infos = request_overrides or {}, VideoInfo(source=self.source), yieldtimerelatedtitle(self.source), []
        episode_id = str(re.compile(r'https?://(?:www\.)?bilibili\.com/bangumi/play/ep(?P<id>\d+)').match(url).group('id'))
        # try parse
        try:
            (resp := self.get('https://api.bilibili.com/pgc/view/web/season', params={'ep_id': episode_id}, **request_overrides)).raise_for_status()
            result_episodes = safeextractfromdict((raw_data := resp2json(resp=resp)), ['result', 'episodes'], [])
            result_episodes += [ep for item in safeextractfromdict(raw_data, ['result', 'section'], []) for ep in dict(item).get('episodes', [])]
            with taskprogress(description='Possible Multiple Videos Detected >>> Parsing One by One', total=len(result_episodes)) as progress:
                for _, result_episode in enumerate(result_episodes):
                    if (not isinstance(result_episode, dict)) or (str(result_episode['ep_id']) != episode_id): progress.advance(1); continue
                    with suppress(Exception): resp = None; (resp := self.get(f"https://api.bilibili.com/pgc/player/web/v2/playurl?fnval=12240&ep_id={str(result_episode['ep_id'])}", **request_overrides)).raise_for_status()
                    if not locals().get('resp') or not hasattr(locals().get('resp'), 'text'): progress.advance(1); continue
                    (page_raw_data := resp2json(resp=resp))['pgc/view/web/season'] = copy.deepcopy(raw_data)
                    (video_page_info := copy.deepcopy(video_info)).update(dict(raw_data=page_raw_data))
                    formats = [{'url': item.get('baseUrl') or item.get('base_url') or item.get('url'), 'filesize': item.get('size') or 0, 'width': item.get('width') or 0, 'height': item.get('height') or 0} for item in page_raw_data['result']['video_info']['dash']['video'] if isinstance(item, dict)]
                    formats: list[dict] = [item for item in sorted(formats, key=lambda x: (x["width"]*x["height"], x["filesize"]), reverse=True) if item.get('url')]
                    video_page_info.update(dict(download_url=(download_url := formats[0]['url'])))
                    video_title = legalizestring(result_episode.get('share_copy') or result_episode.get('show_title') or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
                    guess_video_ext_result = FileTypeSniffer.getfileextensionfromurl(url=download_url, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
                    if (ext := guess_video_ext_result['ext'] if guess_video_ext_result['ext'] and guess_video_ext_result['ext'] != 'NULL' else video_page_info['ext']) in ['m4s']: ext = 'mp4'
                    video_page_info.update(dict(title=video_title, save_path=os.path.join(self.work_dir, self.source, f'{video_title}.{ext}'), ext=ext, guess_video_ext_result=guess_video_ext_result, identifier=episode_id, cover_url=safeextractfromdict(result_episode, ['cover'], None)))
                    audio_formats = [{'url': item.get('baseUrl') or item.get('base_url') or item.get('url'), 'filesize': item.get('size') or 0} for item in (safeextractfromdict(page_raw_data, ['result', 'video_info', 'dash', 'dolby', 'audio'], []) + safeextractfromdict(page_raw_data, ['result', 'video_info', 'dash', 'audio'], [])) if isinstance(item, dict)]
                    audio_formats: list[dict] = [item for item in sorted(audio_formats, key=lambda x: x["filesize"], reverse=True) if item.get('url')]
                    if len(audio_formats) == 0: video_infos.append(video_page_info); progress.advance(1); continue
                    guess_audio_ext_result = FileTypeSniffer.getfileextensionfromurl(url=audio_formats[0]['url'], headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
                    if (audio_ext := guess_audio_ext_result['ext'] if guess_audio_ext_result['ext'] and guess_audio_ext_result['ext'] != 'NULL' else video_info.audio_ext) in ['m4s']: audio_ext = 'm4a'
                    video_page_info.update(dict(audio_download_url=audio_formats[0]['url'], guess_audio_ext_result=guess_audio_ext_result, audio_ext=audio_ext, audio_save_path=os.path.join(self.work_dir, self.source, f'{video_title}.audio.{audio_ext}'))); video_infos.append(video_page_info); progress.advance(1)
        except Exception as err:
            video_info.update(dict(err_msg=(err_msg := f'{self.source}._parsefrombangumiepurl >>> {url} (Error: {err})'))); video_infos.append(video_info)
            self.logger_handle.error(err_msg, disable_print=self.disable_print)
        # return
        return video_infos
    '''_parsefrombangumissurl'''
    def _parsefrombangumissurl(self, url: str, request_overrides: dict = None):
        # prepare
        if not self.belongto(url=url): return []
        request_overrides, video_info, null_backup_title, video_infos = request_overrides or {}, VideoInfo(source=self.source), yieldtimerelatedtitle(self.source), []
        ss_id = str(re.compile(r'(?x)https?://(?:www\.)?bilibili\.com/bangumi/play/ss(?P<id>\d+)').match(url).group('id'))
        # try parse
        try:
            (resp := self.get('https://api.bilibili.com/pgc/web/season/section', params={'season_id': ss_id}, **request_overrides)).raise_for_status()
            result_episodes: list[dict] = safeextractfromdict((raw_data := resp2json(resp=resp)), ['result', 'main_section', 'episodes'], [])
            result_episodes += [ep for item in safeextractfromdict(raw_data, ['result', 'section'], []) if isinstance(item, dict) for ep in item.get('episodes', [])]
            with taskprogress(description='Possible Multiple Videos Detected >>> Parsing One by One', total=len(result_episodes)) as progress:
                for _, result_episode in enumerate(result_episodes):
                    with suppress(Exception): resp = None; (resp := self.get(f"https://api.bilibili.com/pgc/player/web/v2/playurl?fnval=12240&ep_id={result_episode['id']}", **request_overrides)).raise_for_status()
                    if not locals().get('resp') or not hasattr(locals().get('resp'), 'text'): progress.advance(1); continue
                    (page_raw_data := resp2json(resp=resp))['pgc/web/season/section'] = copy.deepcopy(raw_data)
                    if not safeextractfromdict(page_raw_data, ['result', 'video_info', 'dash', 'video'], []): progress.advance(1); continue
                    (video_page_info := copy.deepcopy(video_info)).update(dict(raw_data=page_raw_data))
                    formats = [{'url': item.get('baseUrl') or item.get('base_url'), 'filesize': item.get('size'), 'width': item.get('width'), 'height': item.get('height')} for item in page_raw_data['result']['video_info']['dash']['video'] if isinstance(item, dict)]
                    formats: list[dict] = [item for item in sorted(formats, key=lambda x: (x["width"]*x["height"], x["filesize"]), reverse=True) if item.get('url')]
                    video_page_info.update(dict(download_url=(download_url := formats[0]['url'])))
                    video_title = legalizestring(result_episode.get('long_title') or result_episode.get('title') or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
                    guess_video_ext_result = FileTypeSniffer.getfileextensionfromurl(url=download_url, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
                    if (ext := guess_video_ext_result['ext'] if guess_video_ext_result['ext'] and guess_video_ext_result['ext'] != 'NULL' else video_page_info['ext']) in ['m4s']: ext = 'mp4'
                    video_page_info.update(dict(title=video_title, save_path=os.path.join(self.work_dir, self.source, f'{video_title}.{ext}'), ext=ext, guess_video_ext_result=guess_video_ext_result, identifier=result_episode['id'], cover_url=safeextractfromdict(result_episode, ['cover'], None)))
                    audio_formats = [{'url': item.get('baseUrl') or item.get('base_url') or item.get('url'), 'filesize': item.get('size') or 0} for item in (safeextractfromdict(page_raw_data, ['result', 'video_info', 'dash', 'dolby', 'audio'], []) + safeextractfromdict(page_raw_data, ['result', 'video_info', 'dash', 'audio'], [])) if isinstance(item, dict)]
                    audio_formats: list[dict] = [item for item in sorted(audio_formats, key=lambda x: x["filesize"], reverse=True) if item.get('url')]
                    if len(audio_formats) == 0: video_infos.append(video_page_info); progress.advance(1); continue
                    guess_audio_ext_result = FileTypeSniffer.getfileextensionfromurl(url=audio_formats[0]['url'], headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
                    if (audio_ext := guess_audio_ext_result['ext'] if guess_audio_ext_result['ext'] and guess_audio_ext_result['ext'] != 'NULL' else video_info.audio_ext) in ['m4s']: audio_ext = 'm4a'
                    video_page_info.update(dict(audio_download_url=audio_formats[0]['url'], guess_audio_ext_result=guess_audio_ext_result, audio_ext=audio_ext, audio_save_path=os.path.join(self.work_dir, self.source, f'{video_title}.audio.{audio_ext}'))); video_infos.append(video_page_info); progress.advance(1)
        except Exception as err:
            video_info.update(dict(err_msg=(err_msg := f'{self.source}._parsefrombangumissurl >>> {url} (Error: {err})'))); video_infos.append(video_info)
            self.logger_handle.error(err_msg, disable_print=self.disable_print)
        # return
        return video_infos
    '''_parsefromcheeseepurl'''
    def _parsefromcheeseepurl(self, url: str, request_overrides: dict = None):
        # prepare
        if not self.belongto(url=url): return []
        request_overrides, video_info, null_backup_title, video_infos = request_overrides or {}, VideoInfo(source=self.source), yieldtimerelatedtitle(self.source), []
        episode_id = str(re.compile(r'https?://(?:www\.)?bilibili\.com/cheese/play/ep(?P<id>\d+)').match(url).group('id'))
        # try parse
        try:
            (resp := self.get(f"https://api.bilibili.com/pugv/view/web/season?ep_id={episode_id}", **request_overrides)).raise_for_status()
            result_episodes = (raw_data := resp2json(resp=resp))['data']['episodes']
            with taskprogress(description='Possible Multiple Videos Detected >>> Parsing One by One', total=len(result_episodes)) as progress:
                for _, result_episode in enumerate(result_episodes):
                    if (not isinstance(result_episode, dict)) or (str(result_episode['id']) != episode_id): progress.advance(1); continue
                    with suppress(Exception): resp = None; (resp := self.get('https://api.bilibili.com/pugv/player/web/playurl', params={'avid': result_episode['aid'], 'cid': result_episode['cid'], 'ep_id': episode_id, 'fnval': 16, 'fourk': 1}, **request_overrides)).raise_for_status()
                    if not locals().get('resp') or not hasattr(locals().get('resp'), 'text'): progress.advance(1); continue
                    (page_raw_data := resp2json(resp=resp))['pugv/view/web/season'] = copy.deepcopy(raw_data)
                    (video_page_info := copy.deepcopy(video_info)).update(dict(raw_data=page_raw_data))
                    formats = [{'url': item.get('baseUrl') or item.get('base_url') or item.get('url'), 'filesize': item.get('size') or 0, 'width': item.get('width') or 0, 'height': item.get('height') or 0} for item in page_raw_data['data']['dash']['video'] if isinstance(item, dict)]
                    formats: list[dict] = [item for item in sorted(formats, key=lambda x: (x["width"]*x["height"], x["filesize"]), reverse=True) if item.get('url')]
                    video_page_info.update(dict(download_url=(download_url := formats[0]['url'])))
                    video_title = legalizestring(result_episode.get('title') or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
                    guess_video_ext_result = FileTypeSniffer.getfileextensionfromurl(url=download_url, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
                    if (ext := guess_video_ext_result['ext'] if guess_video_ext_result['ext'] and guess_video_ext_result['ext'] != 'NULL' else video_page_info['ext']) in ['m4s']: ext = 'mp4'
                    video_page_info.update(dict(title=video_title, save_path=os.path.join(self.work_dir, self.source, f'{video_title}.{ext}'), ext=ext, guess_video_ext_result=guess_video_ext_result, identifier=episode_id, cover_url=safeextractfromdict(result_episode, ['cover'], None)))
                    audio_formats = [{'url': item.get('baseUrl') or item.get('base_url') or item.get('url'), 'filesize': item.get('size') or 0} for item in safeextractfromdict(page_raw_data, ['data', 'dash', 'audio'], []) if isinstance(item, dict)]
                    audio_formats: list[dict] = [item for item in sorted(audio_formats, key=lambda x: x["filesize"], reverse=True) if item.get('url')]
                    if len(audio_formats) == 0: video_infos.append(video_page_info); progress.advance(1); continue
                    guess_audio_ext_result = FileTypeSniffer.getfileextensionfromurl(url=audio_formats[0]['url'], headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies)
                    if (audio_ext := guess_audio_ext_result['ext'] if guess_audio_ext_result['ext'] and guess_audio_ext_result['ext'] != 'NULL' else video_info.audio_ext) in ['m4s']: audio_ext = 'm4a'
                    video_page_info.update(dict(audio_download_url=audio_formats[0]['url'], guess_audio_ext_result=guess_audio_ext_result, audio_ext=audio_ext, audio_save_path=os.path.join(self.work_dir, self.source, f'{video_title}.audio.{audio_ext}'))); video_infos.append(video_page_info); progress.advance(1)
        except Exception as err:
            video_info.update(dict(err_msg=(err_msg := f'{self.source}._parsefromcheeseepurl >>> {url} (Error: {err})'))); video_infos.append(video_info)
            self.logger_handle.error(err_msg, disable_print=self.disable_print)
        # return
        return video_infos
    '''_getredirecturl'''
    def _getredirecturl(self, url: str, aid: str, request_overrides: dict = None):
        with suppress(Exception): (resp := self.get("https://api.bilibili.com/x/web-interface/view", params={"aid": aid}, **request_overrides)).raise_for_status(); return self.get(resp2json(resp=resp)['data']['redirect_url'], allow_redirects=True, **request_overrides).url
        return url
    '''parsefromurl'''
    @useparseheaderscookies
    def parsefromurl(self, url: str, request_overrides: dict = None):
        # init
        request_overrides = request_overrides or {}
        if not self.belongto(url=url): return []
        with suppress(Exception): url = self.get(url, allow_redirects=True, **request_overrides).url
        # common url
        pattern = re.compile(r'https?://(?:www\.)?bilibili\.com/(?:video/|festival/[^/?#]+\?(?:[^#]*&)?bvid=)(?P<prefix>[aAbB][vV])(?P<id>[^/?#&]+)')
        if (m := pattern.match(url)) and (m.group('prefix').upper() in ('AV')): url = self._getredirecturl(url, m.group('id'), request_overrides)
        if (m := pattern.match(url)): video_id, prefix = m.group('id', 'prefix')
        if m and video_id and prefix: return self._parsefromcommonurl(url, request_overrides=request_overrides)
        # bangumi ep url
        pattern = re.compile(r'https?://(?:www\.)?bilibili\.com/bangumi/play/ep(?P<id>\d+)')
        if (m := pattern.match(url)): episode_id = m.group('id')
        if m and episode_id: return self._parsefrombangumiepurl(url, request_overrides=request_overrides)
        # bangumi ss url
        pattern = re.compile(r'(?x)https?://(?:www\.)?bilibili\.com/bangumi/play/ss(?P<id>\d+)')
        if (m := pattern.match(url)): ss_id = m.group('id')
        if m and ss_id: return self._parsefrombangumissurl(url, request_overrides=request_overrides)
        # cheese ep url
        pattern = re.compile(r'https?://(?:www\.)?bilibili\.com/cheese/play/ep(?P<id>\d+)')
        if (m := pattern.match(url)): episode_id = m.group('id')
        if m and episode_id: return self._parsefromcheeseepurl(url, request_overrides=request_overrides)
        # not match all, fail to parse
        return [VideoInfo(source=self.source)]
    '''belongto'''
    @staticmethod
    def belongto(url: str, valid_domains: list[str] | set[str] = None):
        valid_domains = set(valid_domains or []) | BILIBILI_SUFFIXES
        return BaseVideoClient.belongto(url, valid_domains)