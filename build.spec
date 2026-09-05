# -*- mode: python ; coding: utf-8 -*-
'''
Function:
    PyInstaller spec for the 全能下载器 desktop shell (Windows / Edge WebView2)
Usage:
    pyinstaller build.spec --noconfirm   (run from the project root)
Author:
    CodeBuddy
'''
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

PROJECT_ROOT = os.path.abspath(SPECPATH)               # VideoDownLoad/
APP_DIR = os.path.join(PROJECT_ROOT, 'app')            # desktop shell package
ENGINE_SRC = os.path.join(PROJECT_ROOT, 'engine')      # engine repo root (contains vd/)

APP_NAME = 'VideoDLDesktop'

'''data files'''
datas = [(os.path.join(APP_DIR, 'web'), 'web')]

# NOTE: collect_data_files('vd') cannot be used here because the engine package lives
# outside of sys.path at spec-evaluation time. Walk the source tree instead and keep the
# same relative layout (vd/modules/js/...) inside the bundle.
# We deliberately skip .py files here so PyInstaller does NOT compile them into PYZ; the
# platform/common parser modules are added separately as DATA files so the frozen exe
# can still `importlib.import_module('vd.modules.sources.bilibili')` from the bundle
# root without bloating PYZ (which otherwise breaks WebView2 init).
VD_PKG = os.path.join(ENGINE_SRC, 'vd')
_packaged = 0
if os.path.isdir(VD_PKG):
    for _root, _dirs, _files in os.walk(VD_PKG):
        if '__pycache__' in _root:
            continue
        _rel = os.path.relpath(_root, ENGINE_SRC)
        for _file in _files:
            if _file.endswith(('.pyc', '.pyo')):
                continue
            _src = os.path.join(_root, _file)
            _dst = os.path.join(_rel, _file)
            if _file.endswith('.py'):
                _rel_n = _rel.replace('\\', '/')
                # only copy the parser source files; keep the heavy `vd/__init__.py`,
                # `vd/vd.py`, etc. inside PYZ so the package still imports cleanly.
                # NB: PyInstaller treats `datas` entries whose destination has a file
                # extension as a *directory* (it appends the basename again), so we
                # pass the destination directory path here and PyInstaller drops each
                # parser straight into the matching sources/common folder.
                if _rel_n.startswith('vd/modules/sources') and not _rel_n.endswith('__init__.py'):
                    datas.append((_src, 'vd/modules/sources'))
                    _packaged += 1
                elif _rel_n.startswith('vd/modules/common') and not _rel_n.endswith('__init__.py'):
                    datas.append((_src, 'vd/modules/common'))
                    _packaged += 1
                else:
                    continue  # leave to PYZ
            else:
                datas.append((_src, _dst))
                _packaged += 1
print(f'[spec] packaged {_packaged} non-python resource file(s) from {VD_PKG} (incl. lazy parser .py sources)')
datas += collect_data_files('webview')          # WebView2 / WinForms interop assemblies
datas += collect_data_files('tldextract')       # .tld_set_snapshot
datas += collect_data_files('fake_useragent')   # data/browsers.jsonl

'''node runtime: we only need node.exe, the bundled npm tree is huge and never used'''
try:
    import nodejs_wheel
    _node_exe = os.path.join(os.path.dirname(nodejs_wheel.__file__), 'node.exe')
    if os.path.exists(_node_exe):
        datas += [(_node_exe, 'nodejs_runtime')]
except Exception as _err:
    print(f'[spec] node runtime not bundled: {_err}')

'''bundled external tools: N_m3u8DL-RE, aria2c, etc.'''
_bundled_bin = os.path.join(PROJECT_ROOT, 'bin')
if os.path.isdir(_bundled_bin):
    datas.append((_bundled_bin, 'bin'))
    print(f'[spec] bundled tools dir: {_bundled_bin}')

'''hidden imports: many parsers and browser drivers import lazily.
NOTE: we deliberately strip the eager `from .xxx import XxxVideoClient` lines
out of `vd.modules.sources/__init__.py` and `vd.modules.common/__init__.py`
to enable lazy parser loading. This means PyInstaller cannot discover the parser
sub-modules via `collect_submodules`, so we have to list them by hand here.'''
hiddenimports = ['backend.api', 'backend.core', 'backend.progress']
hiddenimports += collect_submodules('vd')
hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('DrissionPage')
hiddenimports += collect_submodules('yt_dlp')
hiddenimports += ['curl_cffi', 'freeproxy', 'clr_loader', 'pythonnet',
                  # transitive runtime deps that PyInstaller's Analysis pass may
                  # miss when `collect_submodules('vd')` fails (e.g. because
                  # the build env lacks Cryptodome / platformdirs). Only list
                  # actual importable names here — PyInstaller rejects bare
                  # package names that aren't importable as modules.
                  'pathvalidate', 'platformdirs', 'm3u8', 'Cython',
                  'emoji', 'fake_useragent', 'tldextract', 'h2']

'''binaries'''
binaries = collect_dynamic_libs('webview')
binaries += collect_dynamic_libs('curl_cffi')

'''modules that are never used by this app'''
excludes = ['tkinter', 'PyQt5', 'PySide2', 'PySide6', 'pytest', 'IPython', 'notebook']

block_cipher = None

a = Analysis(
    [os.path.join(APP_DIR, 'app.py')],
    pathex=[APP_DIR, ENGINE_SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[os.path.join(APP_DIR, 'pyinstaller_hooks')],
    runtime_hooks=[os.path.join(APP_DIR, 'pyinstaller_hooks', 'runtime_setup.py')],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(APP_DIR, 'assets', 'icon.ico') if os.path.exists(os.path.join(APP_DIR, 'assets', 'icon.ico')) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
