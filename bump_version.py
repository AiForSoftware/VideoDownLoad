'''
Function:
    构建前自动递增版本号 —— 约定：每次打包自动 +1，永不手工改 APP_VERSION
Usage:
    python bump_version.py                 # patch +1   1.1.0 -> 1.1.1
    python bump_version.py --minor         #            1.1.0 -> 1.2.0
    python bump_version.py --major         #            1.1.0 -> 2.0.0
    python bump_version.py --set 2.0.0     # 直接指定版本（也用于回退）
    python bump_version.py --current       # 只打印当前版本，不写任何文件
Design:
    * `version.txt`（项目根，纯文本）是版本号的唯一真源；
    * 递增后同步写入 `app/app.py` 的 `APP_VERSION`（打包产物里仍是硬编码常量，
      frozen 运行时不依赖任何外部文件）；
    * 日志走 stderr、新版本号走 stdout，便于构建脚本捕获；
    * 该版本号同时用于 SoftwareTracker 的安装/升级上报（`upgrade` 事件靠它区分）。
Author:
    CodeBuddy
'''
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / 'version.txt'
APP_ENTRY = ROOT / 'app' / 'app.py'

'''只匹配 app.py 里的定义行，避免误伤 f-string 里的使用处'''
APP_VERSION_RE = re.compile(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", re.M)
SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def read_current() -> str:
    '''当前版本：优先 version.txt，缺失时回退到 app.py 里的 APP_VERSION'''
    try:
        value = VERSION_FILE.read_text(encoding='utf-8').strip()
        if SEMVER_RE.match(value):
            return value
    except OSError:
        pass
    try:
        match = APP_VERSION_RE.search(APP_ENTRY.read_text(encoding='utf-8'))
        if match:
            return match.group(1)
    except OSError:
        pass
    return '0.0.0'


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(part_value) for part_value in version.split('.'))
    if part == 'major':
        return f'{major + 1}.0.0'
    if part == 'minor':
        return f'{major}.{minor + 1}.0'
    return f'{major}.{minor}.{patch + 1}'


def write_version(version: str) -> bool:
    '''写 version.txt 并同步 app.py 的 APP_VERSION；返回 app.py 是否更新成功'''
    VERSION_FILE.write_text(f'{version}\n', encoding='utf-8')
    try:
        source = APP_ENTRY.read_text(encoding='utf-8')
    except OSError as err:
        _log(f'[bump] 警告：无法读取 {APP_ENTRY}: {err}')
        return False
    new_source, count = APP_VERSION_RE.subn(f"APP_VERSION = '{version}'", source, count=1)
    if count == 0:
        _log(f'[bump] 警告：{APP_ENTRY} 里没找到 APP_VERSION 定义，只更新了 version.txt')
        return False
    APP_ENTRY.write_text(new_source, encoding='utf-8')
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description='构建前自动递增版本号')
    parser.add_argument('--major', action='store_true', help='MAJOR +1（不兼容变更）')
    parser.add_argument('--minor', action='store_true', help='MINOR +1（功能新增）')
    parser.add_argument('--set', dest='set_version', help='直接指定版本，如 2.0.0')
    parser.add_argument('--current', action='store_true', help='只打印当前版本，不写文件')
    args = parser.parse_args()

    current = read_current()
    if args.current:
        print(current)
        return 0

    if args.set_version:
        if not SEMVER_RE.match(args.set_version):
            _log(f"[bump] 版本号必须是 MAJOR.MINOR.PATCH，收到 {args.set_version!r}")
            return 1
        new = args.set_version
    else:
        part = 'major' if args.major else ('minor' if args.minor else 'patch')
        new = bump(current, part)

    write_version(new)
    _log(f'[bump] {current} -> {new}')
    print(new)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
