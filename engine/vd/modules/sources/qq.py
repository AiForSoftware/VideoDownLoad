'''
Function:
    Implementation of TencentVideoClient (腾讯视频 v.qq.com)
'''
import os
import re
import copy
from urllib.parse import urlsplit
import json_repair
from .base import BaseVideoClient
from ..utils import legalizestring, useparseheaderscookies, yieldtimerelatedtitle, safeextractfromdict, FileTypeSniffer, VideoInfo, LoggerHandle


'''TencentVideoClient'''
class TencentVideoClient(BaseVideoClient):
    source = 'TencentVideoClient'
    GETINFO_URL = 'https://vv.video.qq.com/getinfo'
    # /x/cover/<cover>/<VID>.html 与 /x/page/<VID>.html：VID 恒为 ".html" 前的
    # 最后一段。cover 段（mzc00200seo6p1w）不带 .html，所以不会被误匹配 —— 早先
    # 用 `/cover/[^/]*?(...)` 的写法跨不过 cover 后的 "/"，永远取不到 VID。
    VID_FROM_URL_RE = re.compile(r'/([a-zA-Z0-9]{11,})\.html', re.I)
    VID_SEGMENT_RE = re.compile(r'^[a-zA-Z0-9]{11,}$')
    VID_FROM_PAGE_RE = re.compile(r'(?:"vid"|vid=)\s*[:=]\s*"?([a-zA-Z0-9]{11,})', re.I)
    JSONP_RE = re.compile(r'=\s*(\{.*\})', re.S)
    def __init__(self, **kwargs):
        super(TencentVideoClient, self).__init__(**kwargs)
        self.default_parse_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36', 'Referer': 'https://v.qq.com/'}
        self.default_download_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36', 'Referer': 'https://v.qq.com/'}
        self.default_headers = self.default_parse_headers
        self._initsession()
    '''_extractvid'''
    def _extractvid(self, url: str, request_overrides: dict) -> str:
        if (m := TencentVideoClient.VID_FROM_URL_RE.search(url)):
            return m.group(1)
        # 无 .html 后缀时（/x/page/<VID>）取路径最后一段
        seg = [s for s in (urlsplit(url).path or '').split('/') if s]
        if seg and TencentVideoClient.VID_SEGMENT_RE.match(seg[-1]):
            return seg[-1]
        # 短链 / 新版落地页拿不到 vid 时才回落到抓页面
        (resp := self.get(url, **request_overrides)).raise_for_status()
        if (m := TencentVideoClient.VID_FROM_PAGE_RE.search(resp.text)):
            return m.group(1)
        return ''
    '''_getinfo'''
    def _getinfo(self, vid: str, defn: str, request_overrides: dict) -> dict:
        params = dict(vid=vid, platform='101001', charge='0', otype='json', defn=defn, dtype='1', sdtfrom='v1010', host='v.qq.com')
        ck = list(request_overrides.get('cookies', {}).keys())
        LoggerHandle.info(f'[TencentVideoClient] getinfo request vid={vid} defn={defn} cookies_keys={ck}')
        (resp := self.get(TencentVideoClient.GETINFO_URL, params=params, **request_overrides)).raise_for_status()
        text = (resp.text or '').strip()
        LoggerHandle.info(f'[TencentVideoClient] getinfo response len={len(text)} snippet={text[:600]!r}')
        # 响应是 jsonp：QZOutputJson=({...});
        (m := TencentVideoClient.JSONP_RE.search(text))
        data = json_repair.loads(m.group(1) if m else text)
        # 腾讯接口级错误（em/exem 非 0）：如 em:69/exem:2 = 需会员/鉴权(ckey)。
        # 这类视频光有登录 Cookie 不够，必须 ckey 签名，避免误导成"未登录"。
        if isinstance(data, dict) and (data.get('em') not in (0, None) or data.get('exem') not in (0, None)):
            raise RuntimeError(f'腾讯 getinfo 拒绝：em={data.get("em")}, exem={data.get("exem")}, msg={data.get("msg")}'
                               f' —— 该视频可能需要腾讯会员/鉴权(ckey)，当前版本暂不支持此类视频')
        return data
    '''_builddownloadurl'''
    @staticmethod
    def _builddownloadurl(vi: dict) -> str:
        uis = safeextractfromdict(vi, ['ul', 'ui'], []) or []
        base_url = ((uis[0] or {}).get('url') or '') if uis else ''
        fn, fvkey = vi.get('fn') or '', vi.get('fvkey') or ''
        if not (base_url and fn):
            return ''
        # ponytail: 只拼单段片源。多段片源（fn 形如 a.p201.1.mp4，需按分段
        # 逐段拼 vkey 再合并）不支持，天花板见 README 已知边界。
        return f'{base_url}{fn}?vkey={fvkey}'
    '''_pack'''
    def _pack(self, vid: str, vi: dict, label: str, request_overrides: dict, null_backup_title: str, video_info: VideoInfo) -> VideoInfo:
        title = legalizestring(vi.get('ti') or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
        cover_url = safeextractfromdict(vi, ['cl', 'pic'], None)
        _vi = copy.deepcopy(video_info)
        download_url = TencentVideoClient._builddownloadurl(vi)
        _guess = FileTypeSniffer.getfileextensionfromurl(url=download_url, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies, skip_urllib_parse=True)
        _ext = _guess['ext'] if _guess['ext'] and _guess['ext'] != 'NULL' else 'mp4'
        _t = f'{title}_{label}'
        _vi.update(dict(download_url=download_url, quality=label, title=_t,
                        save_path=os.path.join(self.work_dir, self.source, f'{_t}.{_ext}'),
                        ext=_ext, guess_video_ext_result=_guess, identifier=f'{vid}-{label}', cover_url=cover_url))
        return _vi
    '''_browser_getinfo'''
    def _browser_getinfo(self, url: str, request_overrides: dict) -> dict:
        """通道2：用 DrissionPage 打开播放页，让腾讯播放器自己发出带 ckey 的
        getinfo 请求并回放其响应。用于会员/需鉴权的视频（通道1 直接 API 会
        em:69/exem:2）。返回 getinfo 响应 dict（含 vl.vi 的 fn/vkey）。"""
        from DrissionPage import ChromiumPage
        import time
        cookies = request_overrides.get('cookies', {}) or {}
        page = ChromiumPage()
        try:
            try: page.get('https://v.qq.com/')
            except Exception: pass
            for k, v in cookies.items():
                try: page.set.cookie({'name': k, 'value': v, 'domain': '.qq.com', 'path': '/'})
                except Exception: pass
            page.listen.start('vv.video.qq.com/getinfo')
            try: page.get(url)
            except Exception: pass
            deadline = time.time() + 60
            while time.time() < deadline:
                for pk in page.listen.steps():
                    try: body = str(pk.response.body)
                    except Exception: continue
                    m = TencentVideoClient.JSONP_RE.search(body)
                    if not m: continue
                    data = json_repair.loads(m.group(1))
                    if (data.get('vl') or {}).get('vi') or ((data.get('fl') or {}).get('fi')):
                        return data
                time.sleep(1)
        finally:
            try: page.quit()
            except Exception: pass
        raise RuntimeError('浏览器回放未在 60s 内捕获到有效的腾讯 getinfo 响应')
    '''parsefromurl'''
    @useparseheaderscookies
    def parsefromurl(self, url: str, request_overrides: dict = None):
        # prepare
        if not self.belongto(url=url): return []
        request_overrides, video_info, null_backup_title = request_overrides or {}, VideoInfo(source=self.source), yieldtimerelatedtitle(self.source)
        LoggerHandle.info(f'[TencentVideoClient] parsefromurl start url={url} cookies_keys={list(request_overrides.get("cookies", {}).keys())}')
        try:
            vid = self._extractvid(url, request_overrides)
            if not vid:
                raise RuntimeError(f'未能从链接中提取 vid（支持 /x/cover/... 与 /x/page/... 形态）: {url}')
            video_infos, seen, last_diag = [], set(), ''
            # —— 通道1：直接 API（免费视频）——
            probe = None
            try:
                probe = self._getinfo(vid, 'shd', request_overrides)
                last_diag = f'通道1 接口级拒绝: em={probe.get("em")}, exem={probe.get("exem")}, msg={probe.get("msg")}'
            except Exception as err:
                last_diag = f'通道1 普通 getinfo 失败: {err}'
            if probe and (probe.get('fl') or {}).get('fi'):
                last_diag = ''
                formats = probe['fl']['fi']
                for f in (formats or [{'name': 'shd'}]):
                    defn = f.get('name') or 'shd'
                    label = str(f.get('resolution') or defn).upper()
                    try:
                        data = self._getinfo(vid, defn, request_overrides)
                    except Exception as err:
                        last_diag = f'defn={defn} 请求失败: {err}'
                        continue
                    vi = safeextractfromdict(data, ['vl', 'vi', 0], {}) or {}
                    download_url = TencentVideoClient._builddownloadurl(vi)
                    if not download_url or download_url in seen:
                        last_diag = f'defn={defn} 无有效播放地址或与其它档位重复'
                        continue
                    seen.add(download_url)
                    video_infos.append(self._pack(vid, vi, label, request_overrides, null_backup_title, video_info))
            # —— 通道2：浏览器回放（会员/需 ckey 视频）——
            if not video_infos:
                LoggerHandle.info('[TencentVideoClient] 通道1 未取到，尝试浏览器回放')
                try:
                    bdata = self._browser_getinfo(url, request_overrides)
                    vis = safeextractfromdict(bdata, ['vl', 'vi'], []) or []
                    if isinstance(vis, dict): vis = [vis]
                    for idx, vi in enumerate(vis or []):
                        label = str(vi.get('br') or f'auto{idx}').upper()
                        download_url = TencentVideoClient._builddownloadurl(vi)
                        if not download_url or download_url in seen:
                            continue
                        seen.add(download_url)
                        video_infos.append(self._pack(vid, vi, label, request_overrides, null_backup_title, video_info))
                    if not video_infos:
                        last_diag = '浏览器回放未捕获到有效播放地址'
                except Exception as err:
                    last_diag = f'浏览器回放失败: {err}'
            if not video_infos:
                raise RuntimeError(f'腾讯视频解析失败：所有通道均未取到播放地址（vid={vid}）。诊断: {last_diag}')
        except Exception as err:
            video_info.update(dict(err_msg=(err_msg := f'{self.source}.parsefromurl >>> {url} (Error: {err})')))
            self.logger_handle.error(err_msg, disable_print=self.disable_print)
            video_infos = [video_info]
        # return
        return video_infos
    '''belongto'''
    @staticmethod
    def belongto(url: str, valid_domains: list[str] | set[str] = None):
        # 只认 v.qq.com：泛匹配 "qq.com" 会误吞 weixin.qq.com 等无关域名。
        valid_domains = set(valid_domains or []) | {"v.qq.com"}
        return BaseVideoClient.belongto(url, valid_domains)
