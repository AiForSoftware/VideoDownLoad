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


# This project VERIFIES and maintains three platform parsers
# (see docs/视频解析器构建流程.md): douyin / bilibili / youtube.
# All other upstream platform parsers have been REMOVED — do not re-add them
# blindly, they are unverified and some carry privacy/risk issues
# (see AGENT_BUILD_GUIDE.md §9).
#
# 微信视频号 (WeiXinChannelVideoClient) was REMOVED on 2026-09-08 after being
# proven non-functional end-to-end — do NOT re-add it:
#   1) 直链: 无登录态下 get_feed_info 恒定只返回 9 个卡片元数据字段,
#      feedInfo 里永远没有 videoUrl / decodeKey; 分享页与 finder-preview
#      落地页在 桌面/移动/微信 三种 UA 下均只有 2463 字节 SPA 空壳。
#      实测 4 条路径(分享页 / 落地页 / 接口 5 种参数组合 / 3 个候选接口)全灭。
#   2) 解密: 用真实样本(decode_key=2136343393)跑 10 种 ISAAC64 seed 布局
#      × 2 种 keystream 方向, 全部未解出 ftyp, 算法从未被还原。
#   即: 既拿不到直链, 也解不开密文, 且修复需逆向微信 WASM 的 WxIsaac64 +
#      登录态, 成本极高。详见 docs/视频解析器构建流程.md §微信视频号(已移除)。
#
# NOTE: this list doubles as the LAZY-LOAD MATCH TABLE — `core.py` matches the
# URL hostname against `module_name` (or the class name minus "VideoClient").
# So `module_name` MUST be a substring of the platform's hostname, otherwise
# the parser is never imported. "weixin" matches channels.weixin.qq.com;
# "weixin_channel" would NOT (hostnames contain no underscore).
_EAGER_PARSERS = [
    ('douyin', 'DouyinVideoClient'),
    ('bilibili', 'BilibiliVideoClient'),
    ('youtube', 'YouTubeVideoClient'),
    # 'qq' and not 'vqq': module_name must be a SUBSTRING of the hostname
    # (v.qq.com) and a module name cannot contain a dot, so 'vqq' / 'v.qq'
    # would silently never match.
    ('qq', 'TencentVideoClient'),
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