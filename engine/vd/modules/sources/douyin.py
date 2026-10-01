'''
Function:
    Implementation of DouyinVideoClient
'''
import copy
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
    # 视频 ID 可能出现在这些查询参数里（用户主页模态页用 modal_id，部分分享
    # 落地页用 vid/aweme_id）；路径形态见 PATH_VID_RE。
    VID_QUERY_KEYS = ('modal_id', 'vid', 'aweme_id', 'item_id')
    PATH_VID_RE = re.compile(r"/(?:share/)?(?:video|note|slides)/(\d{6,})")
    PROXY_HINT = '检测到代理不可用（连接被拒绝），请在「设置」中修正代理地址或留空使用直连'
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
            # 提取 aweme_id（视频 ID）。抖音落地页形态很多：主站视频页 /video/{id}、
            # 图文 /note/{id}、用户主页 ?modal_id= 或 ?vid=、短链 v.douyin.com。
            # 查询参数优先解析——用户主页 URL 里第一个数字串往往来自 sec_uid
            # （如 MS4wLjABAAAA… 里的 "4"），盲取 \d+ 会拿到完全错误的 ID。
            vid, _vid_err = self._extractvid(url, request_overrides)
            if not vid:
                raise RuntimeError(f'未能从链接中识别出抖音视频 ID（modal_id / vid）{_vid_err}。请复制完整的视频链接后重试')
            # 解析候选顺序（拿到 play_addr 即停）：
            #   ① 主站视频页 https://www.douyin.com/video/{vid}——最规整的 SSR 入口，
            #      带登录 Cookie 时直接返回完整 play_addr；
            #   ② 原始 URL（用户主页模态页 / 分享页，带 Cookie 时同样直出）；
            #   ③ https://www.iesdouyin.com/share/video/{vid} 分享页；
            #   ④ 以上全灭 → amemv feed 接口兜底（无需签名/登录态，见 _parsefromfeedapi）。
            _candidates, _seen_cands = [], set()
            for _c in (f"https://www.douyin.com/video/{vid}", url, f"https://www.iesdouyin.com/share/video/{vid}"):
                if _c and _c not in _seen_cands: _seen_cands.add(_c); _candidates.append(_c)
            play_uri, video_detail = '', {}
            _last_diag = ''
            # 代理预检：端口都不通就别拿它发请求（每个请求都要等一轮 ProxyError，
            # 表现为“所有平台同时解析不了”，与抖音本身无关）。不通则本次直连。
            _proxy_hint = ''
            if request_overrides.get('proxies'):
                if DouyinVideoClient._proxyreachable(request_overrides['proxies']):
                    pass
                else:
                    request_overrides = {k: v for k, v in request_overrides.items() if k != 'proxies'}
                    _proxy_hint = DouyinVideoClient.PROXY_HINT
            for _src in _candidates:
                try:
                    (resp := self.get(_src, **request_overrides)).raise_for_status()
                except Exception as _e:
                    _last_diag = f'请求 {_src} 失败: {_e}'
                    if DouyinVideoClient._isproxyerror(_e): _proxy_hint = DouyinVideoClient.PROXY_HINT
                    continue
                _router = DouyinVideoClient.ROUTER_DATA_RE.search(resp.text)
                if not _router:
                    _last_diag = f'{_src} 未返回 ROUTER_DATA（可能未登录被反爬）'
                    continue
                _raw = _router.group(1).strip().rstrip("; \n\r\t")
                if not _raw.startswith("{"): _raw = _raw[_raw.find("{"):].rstrip("; \n\r\t") if _raw.find("{") != -1 else _raw
                _data = json_repair.loads(_raw)
                # 验证是否含可用 play_addr（避免拿到空壳页面）
                _uri, _detail = DouyinVideoClient._extractplayuri(_data)
                if _uri:
                    play_uri, video_detail = _uri, _detail
                    break
                _last_diag = f'{_src} 返回数据但不含 play_addr.uri（通常需要先登录抖音）'
            if not play_uri:
                # 兜底：amemv feed 接口（无需签名/登录）。分享页与主站在未登录时
                # 都会被反爬裁剪数据，但该接口直接返回完整 aweme_list（含
                # play_addr.uri），是不依赖登录态的可用解析路径。
                try:
                    video_detail = self._parsefromfeedapi(vid, request_overrides)
                    play_uri = DouyinVideoClient._uriof(video_detail)
                except Exception as _e:
                    video_detail = {}
                    self.logger_handle.error(f'amemv feed 兜底解析失败: {_e}', disable_print=self.disable_print)
                    if DouyinVideoClient._isproxyerror(_e): _proxy_hint = DouyinVideoClient.PROXY_HINT
                if not play_uri:
                    _alt = f"https://www.douyin.com/video/{vid}" if vid else ''
                    _hint_parts = []
                    if '/user/' in url or 'modal_id' in url or re.search(r'[?&]vid=', url):
                        _hint_parts.append('当前是用户主页/模态页链接，已依次尝试主站视频页、原链接、分享页与 feed 接口')
                    _hint_parts.append('四条路径均未能取到 play_addr.uri，通常是未登录抖音被反爬/数据裁剪')
                    if _proxy_hint: _hint_parts.append(_proxy_hint)
                    if _alt:
                        _hint_parts.append(f'请点击顶栏「登录态」按钮登录抖音后重试，或复制直链：{_alt}')
                    _hint = '（' + '；'.join(_hint_parts) + '）'
                    raise RuntimeError(f'未能从抖音页面提取到 video.play_addr.uri{_hint}。诊断: {_last_diag}')
            # IMPORTANT: use ratio=default, NOT ratio=1080p. Douyin's CDN keeps a
            # low-bitrate 1080p transcode for the explicit ratio=1080p requests
            # (measured ~300kbps @1920x1080), while `default` serves the player's
            # primary stream at the same resolution with ~2.4x the bitrate
            # (measured 718kbps). Explicit ratio values are resolution-matched
            # but bitrate-starved — that is why downloads looked much worse than
            # the in-app playback.
            video_title = legalizestring(video_detail.get('desc') or null_backup_title, replace_null_string=null_backup_title).removesuffix('.')
            # 画质档位枚举：ratio=default 是播放器主码率（最高清），但部分用户/场景
            # 想要低清晰度片源（流量/体积优先），因此把 CDN 支持的低档 ratio 一并
            # 枚举出来（每档一个 VideoInfo，探测可用才产出，对齐 B站/YouTube 的
            # 每档一条契约；前端按画质后缀分组，用户可在下载前选择档位）。
            # 注意 ratio=1080p 仍然禁止（那是低码率转码档，见上面的历史注释）。
            video_infos = []
            for _ratio, _label in (('default', '1080P'), ('720p', '720P'), ('540p', '540P')):
                _vi = copy.deepcopy(video_info)
                _u = f"http://www.iesdouyin.com/aweme/v1/play/?video_id={play_uri}&ratio={_ratio}&line=0"
                _guess = FileTypeSniffer.getfileextensionfromurl(url=_u, headers=self.default_download_headers, request_overrides=request_overrides, cookies=self.default_download_cookies, skip_urllib_parse=True)
                _ext = _guess['ext'] if _guess['ext'] and _guess['ext'] != 'NULL' else None
                if not _ext: continue  # 该档 CDN 不可用（404/风控），直接剔除
                _t = f'{video_title}_{_label}'
                _vi.update(dict(download_url=_u, quality=_label, title=_t,
                                save_path=os.path.join(self.work_dir, self.source, f'{_t}.{_ext}'),
                                ext=_ext, guess_video_ext_result=_guess, identifier=f'{vid}-{_label}',
                                cover_url=safeextractfromdict(video_detail, ['video', 'cover', 'url_list', 0], None)))
                video_infos.append(_vi)
            if not video_infos:
                # default 档探测都被风控拦截（或探测请求本身走了不可用的代理）时
                # 仍至少产出一条——URI 已验证存在，这里必须补齐全字段（标题/
                # 保存路径/扩展名），否则前端会拿到一条没有标题、无法落盘的空条目。
                _t = f'{video_title}_1080P'
                _vi = copy.deepcopy(video_info)
                _vi.update(dict(download_url=f"http://www.iesdouyin.com/aweme/v1/play/?video_id={play_uri}&ratio=default&line=0",
                                quality='1080P', title=_t, ext='mp4',
                                save_path=os.path.join(self.work_dir, self.source, f'{_t}.mp4'),
                                identifier=f'{vid}-1080P',
                                cover_url=safeextractfromdict(video_detail, ['video', 'cover', 'url_list', 0], None)))
                video_infos.append(_vi)
        except Exception as err:
            video_info.update(dict(err_msg=(err_msg := f'{self.source}.parsefromurl >>> {url} (Error: {err})')))
            self.logger_handle.error(err_msg, disable_print=self.disable_print)
            video_infos = [video_info]
        # return
        return video_infos
    '''_extractvid'''
    def _extractvid(self, url: str, request_overrides: dict = None) -> tuple:
        # 从链接里识别 aweme_id，返回 (vid, 诊断信息)。
        # 顺序：查询参数（modal_id / vid / aweme_id / item_id）→ 路径 /video/{id}、
        # /note/{id}、/share/video/{id} → 跟随重定向后的最终地址（短链）。
        request_overrides = request_overrides or {}
        for _key in DouyinVideoClient.VID_QUERY_KEYS:
            if (_m := re.search(rf'[?&]{_key}=(\d{{6,}})', url)): return _m.group(1), ''
        if (_m := DouyinVideoClient.PATH_VID_RE.search(url)): return _m.group(1), ''
        _err, location = '', ''
        try:
            (resp := self.get(url, allow_redirects=False, **request_overrides)).raise_for_status()
            location = resp.headers.get('Location') or ''
        except Exception as _e:
            _err = str(_e)
        if not location:
            try:
                (resp := self.get(url, allow_redirects=True, **request_overrides)).raise_for_status()
                location = resp.url or ''
            except Exception as _e:
                _err = str(_e)
        if location:
            if (_m := DouyinVideoClient.PATH_VID_RE.search(location)): return _m.group(1), ''
            # 兜底：取“最长的数字串”，优先 ≥10 位（aweme_id 现为 19 位；
            # sec_uid 里的零散数字通常只有 1~3 位，混进来会解析到错误视频）
            _nums = re.findall(r'\d{10,}', location) or re.findall(r'\d+', location)
            if _nums: return max(_nums, key=len), ''
        return '', (f'（跟随链接跳转失败: {_err[:160]}）' if _err else '')
    '''_uriof'''
    @staticmethod
    def _uriof(detail: dict) -> str:
        # 取 video.play_addr.uri（兼容 play_addr 直接挂在根上的结构）
        if not isinstance(detail, dict): return ''
        _uri = (((detail.get('video') or {}).get('play_addr') or {}).get('uri')
                or (detail.get('play_addr') or {}).get('uri'))
        return _uri if isinstance(_uri, str) and _uri else ''
    '''_deepfindaweme'''
    @staticmethod
    def _deepfindaweme(node, depth: int = 0):
        # 递归找第一个含 video.play_addr.uri 的对象（限深限宽）。
        # 抖音改版时 ROUTER_DATA 的层级会变，深搜能扛住结构位移。
        if depth > 8: return None
        if isinstance(node, dict):
            if DouyinVideoClient._uriof(node): return node
            for _v in list(node.values())[:20]:
                if (_hit := DouyinVideoClient._deepfindaweme(_v, depth + 1)) is not None: return _hit
        elif isinstance(node, list):
            for _v in node[:20]:
                if (_hit := DouyinVideoClient._deepfindaweme(_v, depth + 1)) is not None: return _hit
        return None
    '''_extractplayuri'''
    @staticmethod
    def _extractplayuri(data) -> tuple:
        # 从 ROUTER_DATA 里取 (play_addr.uri, aweme 详情)。先走已知的
        # loaderData → videoInfoRes.item_list 路径，再退化为递归深搜。
        loader_data = safeextractfromdict(data, ['loaderData'], {}) or {}
        for _k, _v in loader_data.items():
            if not isinstance(_v, dict): continue
            _vir = _v.get('videoInfoRes')
            if isinstance(_vir, dict):
                for _item in (_vir.get('item_list') or _vir.get('video_list') or []):
                    if (_uri := DouyinVideoClient._uriof(_item or {})): return _uri, (_item or {})
            if _v.get('aweme_detail'):
                _detail = _v.get('aweme_detail') or {}
                if (_uri := DouyinVideoClient._uriof(_detail)): return _uri, _detail
        _hit = DouyinVideoClient._deepfindaweme(data)
        if _hit is not None: return DouyinVideoClient._uriof(_hit), _hit
        return '', {}
    '''_proxyreachable'''
    @staticmethod
    def _proxyreachable(proxies) -> bool:
        # 对 request_overrides['proxies'] 里的代理做一次 TCP 连通性探测。
        # 目的同上：代理不通时宁可直连，也不要让每条请求都撞一轮 ProxyError。
        try:
            import socket
            from urllib.parse import urlsplit
            _proxy = ''
            if isinstance(proxies, dict):
                _proxy = proxies.get('https') or proxies.get('http') or next(iter(proxies.values()), '')
            elif isinstance(proxies, str):
                _proxy = proxies
            if not _proxy: return True
            _p = urlsplit(_proxy if '://' in _proxy else f'http://{_proxy}')
            _host, _port = _p.hostname or '', _p.port or (443 if _p.scheme == 'https' else 80)
            if not _host: return True
            with socket.create_connection((_host, _port), timeout=2.0): return True
        except Exception:
            return False
    '''_isproxyerror'''
    @staticmethod
    def _isproxyerror(err) -> bool:
        # 代理不可用时 requests 抛 ProxyError（WinError 10061 / 目标计算机积极
        # 拒绝）。所有站点会同时失败，必须单独提示，否则用户只会看到“解析失败”。
        _s = str(err).lower()
        return ('proxyerror' in _s or 'unable to connect to proxy' in _s or '10061' in _s
                or 'tunnel connection failed' in _s or 'failed to establish a new connection' in _s)
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