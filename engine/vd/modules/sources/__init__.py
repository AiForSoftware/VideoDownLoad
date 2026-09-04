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


# This project has VERIFIED and maintains exactly three platform parsers
# (see docs/视频解析器构建流程.md): douyin / bilibili / youtube. All other
# upstream platform parsers have been REMOVED — do not re-add them blindly,
# they are unverified and some carry privacy/risk issues (see LLM_BUILD_GUIDE.md §9).
_EAGER_PARSERS = [
    ('douyin', 'DouyinVideoClient'),
    ('bilibili', 'BilibiliVideoClient'),
    ('youtube', 'YouTubeVideoClient'),
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