'''
Function:
    Implementation of DouyinVideoClient
'''
import os
import re
import json
import json_repair
from .base import BaseVideoClient
from ..utils import legalizestring, useparseheaderscookies, yieldtimerelatedtitle, safeextractfromdict, FileTypeSniffer, VideoInfo


'''DouyinVideoClient'''
class DouyinVideoClient(BaseVideoClient):
    source = 'DouyinVideoClient'
    ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.S | re.I)
    def __init__(self, **kwargs):
        super(DouyinVideoClient, self).__init__(**kwargs)
        self.default_parse_headers = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'}
        self.default_download_headers = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'}
        self.default_headers = self.default_parse_headers
        self._initsession()
    '''parsefromurl'''
    @useparseheaderscookies
    def parsefromurl(self, url: str, request_overrides: dict = None):
        # prepare
        if not self.belongto(url=url): return []
        request_overrides, video_info, null_backup_title = request_overrides or {}, VideoInfo(source=self.source), yieldtimerelatedtitle(self.source)
        # try parse
        try:
            # 提取 aweme_id（视频 ID）。jingxuan 模态页的 modal_id 就是 aweme_id；
            # 其它链接（/video/{id} 等）从重定向后的 location 取数字段。
            _m_modal = re.search(r"modal_id=(\d+)", url)
            if _m_modal:
                vid = _m_modal.group(1)
            else:
                (resp := self.get(url, allow_redirects=False, **request_overrides)).raise_for_status(); location = resp.headers.get("Location")
                if not location: (resp := self.get(url, allow_redirects=True, **request_overrides)).raise_for_status(); location = resp.url
                vid = re.search(r"\d+", location).group(0)
            # 解析候选顺序：
            #   1) 主站优先——抖音主站（含 www.douyin.com/jingxuan?modal_id=XXX）
            #      是 SSR，带登录 cookie 时直接返回完整 play_addr；这类“完整地址”
            #      本就该走主站，而不是降级到 iesdouyin 分享页（分享页未登录常被
            #      反爬裁剪，导致拿不到 play_addr）。
            #   2) 主站拿不到（未登录被裁 / 结构不符）再回退 iesdouyin 分享页。
            _candidates = []
            if 'iesdouyin.com' not in url:
                _candidates.append(url)
            _candidates.append(f"https://www.iesdouyin.com/share/video/{vid}")
            raw_data = None
            _last_diag = ''
            for _src in _candidates:
                try:
                    (resp := self.get(_src, **request_overrides)).raise_for_status()
                except Exception as _e:
                    _last_diag = f'请求 {_src} 失败: {_e}'
                    continue
                _router = DouyinVideoClient.ROUTER_DATA_RE.search(resp.text)
                if not _router:
                    _last_diag = f'{_src} 未返回 ROUTER_DATA（可能未登录被反爬）'
                    continue
                _raw = _router.group(1).strip().rstrip("; \n\r\t")
                if not _raw.startswith("{"): _raw = _raw[_raw.find("{"):].rstrip("; \n\r\t") if _raw.find("{") != -1 else _raw
                _data = json_repair.loads(_raw)
                # 验证是否含可用 play_addr（避免拿到空壳页面）
                _ld = safeextractfromdict(_data, ['loaderData'], {}) or {}
                _detail = {}
                for _k, _v in _ld.items():
                    if not isinstance(_v, dict): continue
                    _vir = _v.get('videoInfoRes')
                    if isinstance(_vir, dict):
                        _items = _vir.get('item_list') or _vir.get('video_list') or []
                        if _items: _detail = _items[0] or {}; break
                    if _v.get('aweme_detail'): _detail = _v.get('aweme_detail') or {}; break
                _uri = (((_detail.get('video') or {}).get('play_addr') or {}).get('uri') or (_detail.get('play_addr') or {}).get('uri'))
                if _uri:
                    raw_data = _data
                    break
                _last_diag = f'{_src} 返回数据但不含 play_addr.uri（通常需要先登录抖音）'
            if raw_data is None:
                # 兜底：amemv feed 接口（无需签名/登录）。分享页与主站在未登录时
                # 都会被反爬裁剪数据，但该接口直接返回完整 aweme_list（含
                # play_addr.uri），是不依赖登录态的可用解析路径。
                try:
                    video_detail = self._parsefromfeedapi(vid, request_overrides)
                except Exception as _e:
                    video_detail = {}
                    self.logger_handle.error(f'amemv feed 兜底解析失败: {_e}', disable_print=self.disable_print)
                _feed_uri = (((video_detail.get('video') or {}).get('play_addr') or {}).get('uri') or (video_detail.get('play_addr') or {}).get('uri'))
                if not _feed_uri:
                    _alt = f"https://www.douyin.com/video/{vid}" if vid else ''
                    _hint_parts = []
                    if '/jingxuan' in url or 'modal_id' in url:
                        _hint_parts.append('当前是 jingxuan 模态页，已优先按主站解析并回退 iesdouyin 分享页')
                    _hint_parts.append('主站/分享页/feed 接口均未能取到 play_addr.uri，通常是未登录抖音被反爬/数据裁剪')
                    if _alt:
                        _hint_parts.append(f'请先在【设置】页登录抖音后重试，或复制直链：{_alt}')
                    _hint = '（' + '；'.join(_hint_parts) + '）'
                    raise RuntimeError(f'未能从抖音页面提取到 video.play_addr.uri{_hint}。诊断: {_last_diag}')
            else:
                # 已确认 raw_data 含有效 play_addr，提取 video_detail
                loader_data = safeextractfromdict(raw_data, ['loaderData'], {}) or {}
                video_detail = {}
                for _k, _v in loader_data.items():
                    if not isinstance(_v, dict): continue
                    _vir = _v.get('videoInfoRes')
                    if isinstance(_vir, dict):
                        _items = _vir.get('item_list') or _vir.get('video_list') or []
                        if _items: video_detail = _items[0] or {}; break
                    if _v.get('aweme_detail'): video_detail = _v.get('aweme_detail') or {}; break
            play_uri = ((video_detail.get('video') or {}).get('play_addr') or {}).get('uri') \
                or ((video_detail.get('play_addr') or {}).get('uri'))
            if not play_uri:
                raise RuntimeError('抖音解析异常：候选源已确认含 play_addr，但二次提取失败')
            # IMPORTANT: use ratio=default, NOT ratio=1080p. Douyin's CDN keeps a
            # low-bitrate 1080p transcode for the explicit ratio=1080p requests
            # (measured ~300kbps @1920x1080), while `default` serves the player's
            # primary stream at the same resolution with ~2.4x the bitrate
            # (measured 718kbps). Explicit ratio values are resolution-matched
            # but bitrate-starved — that is why downloads looked much worse than
            # the in-app playback.
            video_info.update(dict(download_url=(download_url := f"http://www.iesdouyin.com/aweme/v1/play/?video_id={play_uri}&ratio=default&line=0")))
            video_title = legalizestring(video_detail.get('desc') or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
            guess_video_ext_result = FileTypeSniffer.getfileextensionfromurl(url=download_url, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies, skip_urllib_parse=True)
            ext = guess_video_ext_result['ext'] if guess_video_ext_result['ext'] and guess_video_ext_result['ext'] != 'NULL' else video_info.ext
            video_info.update(dict(title=video_title, save_path=os.path.join(self.work_dir, self.source, f'{video_title}.{ext}'), ext=ext, guess_video_ext_result=guess_video_ext_result, identifier=vid, cover_url=safeextractfromdict(video_detail, ['video', 'cover', 'url_list', 0], None)))
        except Exception as err:
            video_info.update(dict(err_msg=(err_msg := f'{self.source}.parsefromurl >>> {url} (Error: {err})')))
            self.logger_handle.error(err_msg, disable_print=self.disable_print)
        # return
        return [video_info]
    '''_parsefromfeedapi'''
    def _parsefromfeedapi(self, vid: str, request_overrides: dict = None) -> dict:
        # amemv feed 接口无需任何签名/登录态。注意：该接口返回的是推荐流，
        # 目标视频命中时会排在首位，但绝不能在未命中时回落到首条（会静默
        # 返回无关视频），必须严格按 aweme_id 精确匹配。
        request_overrides = request_overrides or {}
        feed_url = (f"https://api3-normal-c-lf.amemv.com/aweme/v1/feed/?aweme_id={vid}"
                    f"&version_code=26.2.0&app_name=aweme&channel=App+Store"
                    f"&device_type=iPhone&device_platform=iphone&os_version=16.0&aid=1128")
        (resp := self.get(feed_url, **request_overrides)).raise_for_status()
        aweme_list = resp.json().get('aweme_list') or []
        for item in aweme_list:
            if (item.get('aweme_id') or '') == vid: return item
        return {}
    '''belongto'''
    @staticmethod
    def belongto(url: str, valid_domains: list[str] | set[str] = None):
        valid_domains = set(valid_domains or []) | {"douyin.com", "iesdouyin.com", "douyinvod.com", "amemv.com"}
        return BaseVideoClient.belongto(url, valid_domains)