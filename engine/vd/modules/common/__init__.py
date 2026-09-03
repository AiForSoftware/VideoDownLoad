'''initialize'''
#
# Lazy common-parser loading for the desktop backend. See the long comment in
# `vd.modules.sources.__init__.py` for the rationale. The vd
# command-line tool runs with `VD_LAZY_PARSERS` unset, in which case this
# file eagerly imports every generic parser to keep the CLI behaviour
# unchanged.
#
import os
import importlib
from ..utils import BaseModuleBuilder
from ..sources.base import BaseVideoClient  # noqa: F401  - keeps the metaclass available


_EAGER_COMMON = []


def _eager_register_all():
    for module_name, _class_name in _EAGER_COMMON:
        try:
            importlib.import_module(f'.{module_name}', __name__)
        except Exception:
            pass


'''CommonVideoClientBuilder'''
class CommonVideoClientBuilder(BaseModuleBuilder):
    REGISTERED_MODULES = {}

    def __init__(self, requires_register_modules=None, requires_renew_modules=None):
        if os.environ.get('VD_LAZY_PARSERS') != '1':
            _eager_register_all()
        super().__init__(requires_register_modules=requires_register_modules, requires_renew_modules=requires_renew_modules)


'''BuildCommonVideoClient'''
BuildCommonVideoClient = CommonVideoClientBuilder().build