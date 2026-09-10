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
from ..utils import legalizestring, useparseheaderscookies, yieldtimerelatedtitle, safeextractfromdict, FileTypeSniffer, VideoInfo


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
        (resp := self.get(TencentVideoClient.GETINFO_URL, params=params, **request_overrides)).raise_for_status()
        text = (resp.text or '').strip()
        # 响应是 jsonp：QZOutputJson=({...});
        (m := TencentVideoClient.JSONP_RE.search(text))
        return json_repair.loads(m.group(1) if m else text)
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
    '''parsefromurl'''
    @useparseheaderscookies
    def parsefromurl(self, url: str, request_overrides: dict = None):
        # prepare
        if not self.belongto(url=url): return []
        request_overrides, video_info, null_backup_title = request_overrides or {}, VideoInfo(source=self.source), yieldtimerelatedtitle(self.source)
        try:
            vid = self._extractvid(url, request_overrides)
            if not vid:
                raise RuntimeError(f'未能从链接中提取 vid（支持 /x/cover/... 与 /x/page/... 形态）: {url}')
            video_infos, title, cover_url, seen, last_diag = [], '', None, set(), ''
            # 先探一次拿 fl.fi —— 该片源真实可用的清晰度。腾讯按片源返回档位
            # （实测本片源只有 480P / 720P），硬编码 fhd/shd/hd/sd 会为不存在
            # 的档位白发请求并产出重复条目。每个 defn 各有自己的 fn/vkey。
            probe = self._getinfo(vid, 'shd', request_overrides)
            formats = (probe.get('fl') or {}).get('fi') or []
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
                # 不支持的档位会降级返回同一地址（fhd → shd），按地址去重
                if not download_url or download_url in seen:
                    last_diag = f'defn={defn} 无有效播放地址或与其它档位重复'
                    continue
                seen.add(download_url)
                title = title or legalizestring(vi.get('ti') or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
                cover_url = cover_url or safeextractfromdict(vi, ['cl', 'pic'], None)
                _vi = copy.deepcopy(video_info)
                _guess = FileTypeSniffer.getfileextensionfromurl(url=download_url, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies, skip_urllib_parse=True)
                _ext = _guess['ext'] if _guess['ext'] and _guess['ext'] != 'NULL' else 'mp4'
                _t = f'{title}_{label}'
                _vi.update(dict(download_url=download_url, quality=label, title=_t,
                                save_path=os.path.join(self.work_dir, self.source, f'{_t}.{_ext}'),
                                ext=_ext, guess_video_ext_result=_guess, identifier=f'{vid}-{label}', cover_url=cover_url))
                video_infos.append(_vi)
            if not video_infos:
                raise RuntimeError(f'腾讯视频解析失败：所有清晰度均未取到播放地址（vid={vid}）。诊断: {last_diag}')
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
