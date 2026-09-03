'''initialize'''
#
# Lazy parser loading for the desktop backend.
#
# Originally this file had ~70 `from .bilibili import BilibiliVideoClient`
# statements that registered every platform parser into
# `VideoClientBuilder.REGISTERED_MODULES` at import time. That made the vd
# engine take 3-8 seconds to import, even when the user only pasted a single
# bilibili / youtube / 抖音 URL.
#
# With `VD_LAZY_PARSERS=1` (set by the desktop backend before importing
# vd), `REGISTERED_MODULES` starts empty and parsers are registered
# automatically the first time their `.py` is imported, via the
# `AutoRegisterMeta` metaclass attached to `BaseVideoClient`. Without the
# environment variable we fall back to the original eager-import list so the
# command-line `vd` tool keeps working exactly as before.
#
import os
import importlib
from .base import BaseVideoClient
from ..utils import BaseModuleBuilder


_EAGER_PARSERS = [
    ('iyf', 'IYFVideoClient'), ('abc', 'ABCVideoClient'), ('wwe', 'WWEVideoClient'),
    ('ted', 'TedVideoClient'), ('ku6', 'Ku6VideoClient'), ('c56', 'C56VideoClient'),
    ('ccc', 'CCCVideoClient'), ('pear', 'PearVideoClient'), ('huya', 'HuyaVideoClient'),
    ('sina', 'SinaVideoClient'), ('base', 'BaseVideoClient'), ('mgtv', 'MGTVVideoClient'),
    ('cctv', 'CCTVVideoClient'), ('sohu', 'SohuVideoClient'), ('nuvid', 'NuVidVideoClient'),
    ('tbnuk', 'TBNUKVideoClient'), ('unity', 'UnityVideoClient'), ('acfun', 'AcFunVideoClient'),
    ('xigua', 'XiguaVideoClient'), ('pipix', 'PipixVideoClient'), ('oasis', 'OasisVideoClient'),
    ('weibo', 'WeiboVideoClient'), ('zhihu', 'ZhihuVideoClient'), ('kakao', 'KakaoVideoClient'),
    ('youku', 'YoukuVideoClient'), ('m1905', 'M1905VideoClient'), ('iqiyi', 'IQiyiVideoClient'),
    ('leshi', 'LeshiVideoClient'), ('rutube', 'RutubeVideoClient'), ('youtube', 'YouTubeVideoClient'),
    ('artetv', 'ArteTVVideoClient'), ('sixroom', 'SixRoomVideoClient'), ('foxnews', 'FoxNewsVideoClient'),
    ('meipai', 'MeipaiVideoClient'), ('genius', 'GeniusVideoClient'),
    ('haokan', 'HaokanVideoClient'), ('douyin', 'DouyinVideoClient'), ('kugoumv', 'KugouMVVideoClient'),
    ('open163', 'Open163VideoClient'), ('reddit', 'RedditVideoClient'), ('rednote', 'RednoteVideoClient'),
    ('pipigaoxiao', 'PipigaoxiaoVideoClient'), ('wesing', 'WeSingVideoClient'), ('weishi', 'WeishiVideoClient'),
    ('tencent', 'TencentVideoClient'), ('xuexicn', 'XuexiCNVideoClient'), ('huanqiu', 'HuanQiuVideoClient'),
    ('mingpao', 'MingpaoVideoClient'), ('cctvnews', 'CCTVNewsVideoClient'),
    ('kuaishou', 'KuaishouVideoClient'), ('bilibili', 'BilibiliVideoClient'),
    ('myvideoge', 'MyVideoGeVideoClient'), ('newspicks', 'NewsPicksVideoClient'),
    ('xinhuanet', 'XinhuaNetVideoClient'), ('yinyuetai', 'YinyuetaiVideoClient'),
    ('duxiaoshi', 'DuxiaoshiVideoClient'), ('dongchedi', 'DongchediVideoClient'),
    ('kankannews', 'KanKanNewsVideoClient'), ('baidutieba', 'BaiduTiebaVideoClient'),
    ('eyepetizer', 'EyepetizerVideoClient'), ('chinadaily', 'ChinaDailyVideoClient'),
    ('dailymotion', 'DailyMotionVideoClient'), ('xinpianchang', 'XinpianchangVideoClient'),
    ('orientaldaily', 'OrientalDailyVideoClient'), ('beacon', 'BeaconVideoClient'),
    ('cctalk', 'CCtalkVideoClient'), ('people', 'PeopleVideoClient'),
    ('www163', 'WWW163VideoClient'), ('zuiyou', 'ZuiyouVideoClient'),
]


def _eager_register_all():
    for module_name, _class_name in _EAGER_PARSERS:
        try:
            importlib.import_module(f'.{module_name}', __name__)
        except Exception:
            pass


'''VideoClientBuilder'''
class VideoClientBuilder(BaseModuleBuilder):
    REGISTERED_MODULES = video_clients = {}

    def __init__(self, requires_register_modules=None, requires_renew_modules=None):
        if os.environ.get('VD_LAZY_PARSERS') != '1':
            _eager_register_all()
        super().__init__(requires_register_modules=requires_register_modules, requires_renew_modules=requires_renew_modules)


'''BuildVideoClient'''
BuildVideoClient = VideoClientBuilder().build