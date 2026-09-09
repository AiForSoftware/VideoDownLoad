'''
Function:
    SoftwareTracker 上报客户端 —— SoftwareTracker/sdk/tracker.js (Node) 的 Python 等价实现
Usage:
    from backend.tracker import create_tracker
    tr = create_tracker(version='1.1.0')
    tr.track({'eventType': 'install', 'status': 'success'})   # 发后不管，后台线程
    tr.track_async({'eventType': 'uninstall'})                # 同步等待，卸载程序退出前用
Design:
    1. deviceId 持久化到 <用户配置目录>/software-tracker/device-id，与 Node SDK 使用同一目录、
       同一规则（dev_<hostname>_<毫秒时间戳36进制>），保证同一台机器只算一个设备；
    2. 自动采集系统 / CPU / 内存 / 时区 / 语言，调用方只需补业务字段（eventType、status ...）；
    3. track() 在 daemon 线程里发送（5s 超时），不阻塞主流程、不向外抛异常；
    4. 网络层失败时静默写入本地 queue.json（最多 200 条），下次调用自动补发。
Note:
    服务端接口固定为 POST {serverUrl}/api/report，请求体 {"events":[...]}。
    仅用标准库实现（urllib），不引入第三方依赖，避免拖慢冷启动。
Author:
    CodeBuddy
'''
from __future__ import annotations

import json
import os
import platform
import random
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

'''服务端地址 / 应用标识：可用环境变量覆盖，便于联调与私有化部署'''
DEFAULT_SERVER_URL = 'http://tranker.2wm.top'
DEFAULT_APP_KEY = 'AK_VDDE-UC4RLV'
REPORT_PATH = '/api/report'
TIMEOUT_SECONDS = 5
QUEUE_LIMIT = 200

_BASE36 = '0123456789abcdefghijklmnopqrstuvwxyz'


def _base36(value: int) -> str:
    '''整数转 36 进制字符串（与 JS Number.prototype.toString(36) 同形）'''
    if value <= 0:
        return '0'
    out = ''
    while value:
        out = _BASE36[value % 36] + out
        value //= 36
    return out


def _random36(length: int = 8) -> str:
    return ''.join(random.choice(_BASE36) for _ in range(length))


def gen_event_id() -> str:
    '''事件唯一 ID：E + 毫秒时间戳(36进制) + 随机串'''
    return ('E' + _base36(int(time.time() * 1000)) + _random36()).upper()


def get_storage_dir() -> Path:
    '''设备 ID / 队列存放目录：Windows %APPDATA%、macOS Application Support、Linux ~/.config'''
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or str(Path.home())
    elif sys.platform == 'darwin':
        base = str(Path.home() / 'Library' / 'Application Support')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    directory = Path(base) / 'software-tracker'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except Exception:
        return Path(tempfile.gettempdir())


def resolve_device_id(storage_dir: Optional[Path] = None) -> str:
    '''读取或生成稳定的设备 ID（同一台机器多次安装 / 升级保持不变）'''
    target = (storage_dir or get_storage_dir()) / 'device-id'
    try:
        if target.exists():
            existing = target.read_text(encoding='utf-8').strip()
            if existing:
                return existing
        generated = f'dev_{socket.gethostname()}_{_base36(int(time.time() * 1000))}'
        target.write_text(generated, encoding='utf-8')
        return generated
    except Exception:
        # 无法读写文件时退化为每次生成，保证上报不中断
        return f'dev_{_base36(int(time.time() * 1000))}'


def _os_name() -> str:
    '''系统名：精确到 Windows 10/11、macOS 大版本（后台字典同粒度）'''
    name = platform.system()
    if name == 'Windows':
        try:
            return 'Windows 11' if sys.getwindowsversion().build >= 22000 else 'Windows 10'
        except Exception:
            return 'Windows'
    if name == 'Darwin':
        version = platform.mac_ver()[0]
        return f'macOS {version}' if version else 'macOS'
    return name or 'Linux'


def _arch() -> str:
    '''CPU 架构：统一成 x64 / x86 / arm64（与 Node os.arch() 输出对齐）'''
    machine = (platform.machine() or '').lower()
    return {'amd64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64',
            'i386': 'x86', 'i686': 'x86'}.get(machine, platform.machine() or '')


def _cpu_model() -> str:
    '''CPU 型号：Windows 读注册表，Linux 读 /proc/cpuinfo，其余退回 platform.processor()'''
    if sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                return str(winreg.QueryValueEx(key, 'ProcessorNameString')[0]).strip()
        except Exception:
            pass
    if sys.platform.startswith('linux'):
        try:
            for line in Path('/proc/cpuinfo').read_text(encoding='utf-8', errors='ignore').splitlines():
                if 'model name' in line:
                    return line.split(':', 1)[1].strip()
        except Exception:
            pass
    return platform.processor() or ''


def _memory_gb() -> int:
    '''内存容量（GB，整数）'''
    try:
        import psutil
        return max(1, round(psutil.virtual_memory().total / 1024 ** 3))
    except Exception:
        pass
    try:
        return max(1, round(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1024 ** 3))
    except Exception:
        return 0


def _language() -> str:
    '''系统语言，如 zh-CN'''
    if sys.platform == 'win32':
        try:
            import ctypes
            buffer = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, 85):
                return buffer.value
        except Exception:
            pass
    try:
        import locale
        name = locale.getdefaultlocale()[0]
        if name:
            return name
    except Exception:
        pass
    return os.environ.get('LANG', '')


'''Windows 只提供本地化的时区名，这里映射回 IANA 名（后台按 IANA 展示）'''
_TZ_ALIASES = {
    '中国标准时间': 'Asia/Shanghai', '中国夏令时': 'Asia/Shanghai',
    'China Standard Time': 'Asia/Shanghai', '台北標準時間': 'Asia/Taipei',
}


def _timezone() -> str:
    '''时区：Windows 只返回本地化名（如「中国标准时间」），映射回后台字典的 IANA 名'''
    try:
        name = datetime.now().astimezone().tzname() or ''
    except Exception:
        name = ''
    if not name and time.tzname:
        name = time.tzname[0]
    return _TZ_ALIASES.get(name, name)


def collect_env() -> Dict[str, Any]:
    '''采集设备与系统环境（对应 Node SDK 的 collectEnv）'''
    return {
        'deviceName': socket.gethostname(),
        'osName': _os_name(),
        'osVersion': platform.version() or platform.release(),
        'osArch': _arch(),
        'osLanguage': _language(),
        'timezone': _timezone(),
        'cpu': _cpu_model(),
        'memory': _memory_gb(),
    }


def _post_json(server_url: str, payload: Dict[str, Any], timeout: int = TIMEOUT_SECONDS) -> None:
    '''POST 一个 JSON 到 /api/report；任何网络层异常都向外抛，由调用方降级处理'''
    url = (server_url or '').rstrip('/') + REPORT_PATH
    if not url.startswith(('http://', 'https://')):
        raise ValueError(f'illegal serverUrl: {server_url!r}')
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'Content-Length': str(len(data))},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read(4096)


class Tracker:
    '''上报实例（对应 Node SDK createTracker 的返回值）'''

    def __init__(self, server_url: str, app_key: str, version: str = '', channel: str = 'official',
                 device_id: Optional[str] = None, queue_path: Optional[str] = None):
        if not server_url:
            raise ValueError('缺少 serverUrl')
        if not app_key:
            raise ValueError('缺少 appKey')
        self.server_url = server_url
        self.app_key = app_key
        self.version = version or ''
        self.channel = channel or 'official'
        self.device_id = device_id or resolve_device_id()
        # 环境信息只在创建时采集一次，避免重复开销
        self.env = collect_env()
        self.queue_path = Path(queue_path) if queue_path else get_storage_dir() / 'queue.json'

    '''本地失败队列'''

    def _read_queue(self) -> List[Dict[str, Any]]:
        try:
            if self.queue_path.exists():
                data = json.loads(self.queue_path.read_text(encoding='utf-8') or '[]')
                return data if isinstance(data, list) else []
        except Exception:
            pass  # 队列损坏时直接忽略
        return []

    def _write_queue(self, events: List[Dict[str, Any]]) -> None:
        try:
            self.queue_path.write_text(json.dumps(events[-QUEUE_LIMIT:], ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass  # 写入失败不影响主流程

    def _build_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        '''组装一条完整事件：环境信息 + 业务字段（业务字段可覆盖默认值）'''
        event = dict(self.env)
        event.update({
            'eventId': payload.get('eventId') or gen_event_id(),
            'appKey': self.app_key,
            'version': payload.get('version') or self.version,
            'eventType': payload.get('eventType') or 'install',
            'status': payload.get('status') or 'success',
            'deviceId': self.device_id,
            'channel': payload.get('channel') or self.channel,
            'reportTime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
        event.update({k: v for k, v in payload.items() if v is not None})
        return event

    def _send(self, events: List[Dict[str, Any]]) -> bool:
        '''真正发送：失败则把事件写回本地队列，等待下次补发'''
        if not events:
            return True
        try:
            _post_json(self.server_url, {'events': events})
            self._write_queue([])
            return True
        except Exception:
            self._write_queue(self._read_queue() + events)
            return False

    def track(self, payload: Optional[Dict[str, Any]] = None) -> bool:
        '''上报一条事件（发后不管，立即返回，网络请求在后台线程进行）'''
        payload = payload or {}

        def _run():
            self._send(self._read_queue() + [self._build_event(payload)])

        threading.Thread(target=_run, name='st-track', daemon=True).start()
        return True

    def track_async(self, payload: Optional[Dict[str, Any]] = None) -> bool:
        '''上报一条事件并等待结果（卸载程序退出前使用）'''
        payload = payload or {}
        return self._send(self._read_queue() + [self._build_event(payload)])

    def flush_queue(self) -> None:
        '''仅补发历史失败队列，不产生新事件（队列为空时不做任何网络请求）'''

        def _run():
            pending = self._read_queue()
            if pending:
                self._send(pending)

        threading.Thread(target=_run, name='st-flush', daemon=True).start()


def create_tracker(server_url: Optional[str] = None, app_key: Optional[str] = None,
                   version: str = '', channel: str = 'official', **kwargs) -> Tracker:
    '''创建上报实例；serverUrl / appKey 可用环境变量 VD_TRACKER_SERVER / VD_TRACKER_APP_KEY 覆盖'''
    return Tracker(
        server_url=server_url or os.environ.get('VD_TRACKER_SERVER') or DEFAULT_SERVER_URL,
        app_key=app_key or os.environ.get('VD_TRACKER_APP_KEY') or DEFAULT_APP_KEY,
        version=version,
        channel=channel,
        **kwargs,
    )
