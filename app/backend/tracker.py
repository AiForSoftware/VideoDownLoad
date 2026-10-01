'''
Function:
    SoftwareTracker 上报客户端 —— SoftwareTracker/sdk/tracker.js (Node) v2 的 Python 等价实现
Usage:
    from backend.tracker import create_tracker
    tr = create_tracker(version='1.1.0')
    tr.track({'eventType': 'install', 'status': 'success'})   # 发后不管，后台线程
    tr.track_async({'eventType': 'uninstall'})                # 同步等待，卸载程序退出前用
    tr.flush_queue()                                          # 补发历史失败队列
Design (对齐 SDK v2，2026-10-01):
    1. deviceId 持久化到 <用户配置目录>/software-tracker/device-id，与 Node SDK 使用同一目录、
       同一规则（dev_<hostname>_<毫秒时间戳36进制>），保证同一台机器只算一个设备；
    2. 自动采集系统 / CPU / 内存 / 时区 / 语言，调用方只需补业务字段（eventType、status ...）；
    3. track() 在 daemon 线程里发送（默认 5s 超时），不阻塞主流程、不向外抛异常；
    4. 备用域名降级（fallback_urls）：主地址失联时依次尝试备用地址，域名迁移不再整段丢数据；
    5. 重试 + 退避：网络异常 / HTTP 5xx 重试 retries 次（默认 1，等待 1s/2s/3s…）；
       HTTP 4xx 视为「已送达」（服务端明确拒绝，重试只会堆积脏数据），事件丢弃；
    6. 同一进程内串行发送（锁）：避免并发 track 重复携带同一份队列导致后台重复入库；
    7. 队列过期清理：积压事件带 _queuedAt 时间戳，超过 max_age（默认 7 天）直接丢弃；
    8. 去重模式（dedupe）：同一 eventType+version+channel 只上报一次（先标记再发送，at-most-once）；
    9. 磁盘不可写时退化到进程内内存队列，不静默丢事件；队列文件先写 .tmp 再 rename 防半截。
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
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

'''服务端地址 / 应用标识：可用环境变量覆盖，便于联调与私有化部署'''
DEFAULT_SERVER_URL = 'http://t.2wm.top'
DEFAULT_APP_KEY = 'AK_VDDE-UC4RLV'
REPORT_PATH = '/api/report'
TIMEOUT_SECONDS = 5
RETRIES = 1
QUEUE_LIMIT = 200
MAX_AGE_SECONDS = 7 * 24 * 60 * 60

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


def _read_json(file: Path, fallback: Any) -> Any:
    '''读取 JSON 文件，损坏或不存在时返回兜底值'''
    try:
        if file.exists():
            return json.loads(file.read_text(encoding='utf-8') or json.dumps(fallback))
    except Exception:
        pass  # 文件损坏时直接忽略
    return fallback


def _write_json(file: Path, data: Any) -> bool:
    '''写入 JSON 文件（先写 .tmp 再 rename，避免半截文件；失败不影响主流程）'''
    try:
        tmp = Path(str(file) + '.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, file)
        return True
    except Exception:
        pass
    try:
        file.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return True
    except Exception:
        return False


def _post_json(server_url: str, payload: Dict[str, Any], timeout: int = TIMEOUT_SECONDS) -> int:
    '''POST 一个 JSON 到 /api/report，返回 HTTP 状态码；网络层异常向外抛，由调用方降级处理'''
    url = (server_url or '').rstrip('/') + REPORT_PATH
    if not url.startswith(('http://', 'https://')):
        raise ValueError(f'illegal serverUrl: {server_url!r}')
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'Content-Length': str(len(data))},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(4096)
            return response.status
    except urllib.error.HTTPError as err:
        # 服务端有响应（4xx/5xx）：读掉 body 供日志，状态码交由调用方分类处理
        try:
            err.read(4096)
        except Exception:
            pass
        return err.code


class Tracker:
    '''上报实例（对应 Node SDK createTracker 的返回值）'''

    def __init__(self, server_url: str, app_key: str, version: str = '', channel: str = 'official',
                 device_id: Optional[str] = None, queue_path: Optional[str] = None,
                 fallback_urls: Optional[List[str]] = None, timeout: int = TIMEOUT_SECONDS,
                 retries: int = RETRIES, queue_limit: int = QUEUE_LIMIT,
                 max_age_seconds: int = MAX_AGE_SECONDS, dedupe: bool = False,
                 debug: bool = False, logger: Optional[Callable[[str, str], None]] = None,
                 storage_dir: Optional[Path] = None):
        if not server_url:
            raise ValueError('缺少 serverUrl')
        if not app_key:
            raise ValueError('缺少 appKey')
        '''地址列表：主地址 + 备用地址（去掉空项，主地址仍排首位）'''
        self.url_list = [server_url] + [u for u in (fallback_urls or []) if u]
        self.app_key = app_key
        self.version = version or ''
        self.channel = channel or 'official'
        self.timeout = timeout
        self.retries = max(0, retries)
        self.queue_limit = max(1, queue_limit)
        self.max_age_seconds = max_age_seconds
        self.dedupe = dedupe
        self._debug = debug
        self._logger = logger
        self._send_lock = threading.Lock()  # 串行发送，避免并发重复携带同一份队列
        self._memory_queue: List[Dict[str, Any]] = []  # 磁盘不可写时的兜底队列（仅本进程有效）
        self.device_id = device_id or resolve_device_id(storage_dir)
        # 环境信息只在创建时采集一次，避免重复开销
        self.env = collect_env()
        directory = storage_dir or get_storage_dir()
        self.queue_path = Path(queue_path) if queue_path else directory / 'queue.json'
        self.state_path = directory / 'tracker-state.json'

    def log(self, level: str, message: str) -> None:
        '''日志出口：优先用调用方注入的 logger，debug 模式退回 stderr；日志绝不能影响宿主程序'''
        try:
            if self._logger is not None:
                self._logger(level, f'[SoftwareTracker] {message}')
            elif self._debug:
                print(f'[SoftwareTracker] {level}: {message}', file=sys.stderr)
        except Exception:
            pass

    '''本地失败队列'''

    def _read_queue(self) -> List[Dict[str, Any]]:
        data = _read_json(self.queue_path, [])
        return data if isinstance(data, list) else []

    def _write_queue(self, events: List[Dict[str, Any]]) -> bool:
        return _write_json(self.queue_path, events[-self.queue_limit:])

    '''去重状态（dedupe 模式）：事件指纹 -> 上次上报时间'''

    def _read_state(self) -> Dict[str, Any]:
        state = _read_json(self.state_path, {'reported': {}})
        if not isinstance(state, dict) or not isinstance(state.get('reported'), dict):
            return {'reported': {}}
        return state

    @staticmethod
    def _fingerprint(event: Dict[str, Any]) -> str:
        '''事件指纹：同一 事件类型 + 版本 + 渠道 视为重复'''
        return f"{event.get('eventType') or 'install'}|{event.get('version') or ''}|{event.get('channel') or ''}"

    def reset_dedupe(self) -> None:
        '''清空去重记录（重装 / 换版本需要重新计数时调用）'''
        _write_json(self.state_path, {'reported': {}})

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

    def _try_urls(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        '''依次尝试主地址与各备用地址（每个地址内含重试 + 退避）

        返回 {ok, delivered}：
            ok        —— 服务端返回 2xx，队列可清空
            delivered —— 服务端明确拒绝（4xx，字段错误等），视为已送达、不再重试
        '''
        last_reason = ''
        for i, url in enumerate(self.url_list):
            for attempt in range(self.retries + 1):
                if attempt > 0:
                    time.sleep(min(1 * attempt, 3))
                try:
                    status = _post_json(url, {'events': events}, self.timeout)
                    if 200 <= status < 300:
                        return {'ok': True, 'delivered': True}
                    if 400 <= status < 500:
                        # 请求已被服务端接收并明确判定为非法字段：重试没有意义，只会堆积脏数据
                        last_reason = f'HTTP {status} @ {url}'
                        self.log('warn', f'服务端拒绝（字段问题，事件丢弃）：{last_reason}')
                        return {'ok': False, 'delivered': True}
                    last_reason = f'HTTP {status} @ {url}'
                except Exception as err:
                    last_reason = f'{err} @ {url}'
            if i < len(self.url_list) - 1:
                self.log('warn', f'主地址不可用，降级到备用地址：{self.url_list[i + 1]}')
        return {'ok': False, 'delivered': False, 'reason': last_reason}

    def _send_once(self) -> bool:
        '''把当前队列整体发出：成功/被拒则清空，网络失败则保留等待下次补发'''
        raw = self._read_queue() + self._memory_queue
        if not raw:
            return True
        # 丢弃过期积压事件，避免断网几天后一次性灌入大量历史数据
        now_ms = time.time() * 1000
        events = [e for e in raw
                  if not e.get('_queuedAt') or now_ms - e['_queuedAt'] < self.max_age_seconds * 1000]
        if len(events) != len(raw):
            self.log('warn', f'丢弃过期积压事件 {len(raw) - len(events)} 条')
        if not events:
            self._write_queue([])
            self._memory_queue = []
            return True
        # _queuedAt 是内部标记字段，上报前剥掉避免污染事件体
        payload_events = [{k: v for k, v in e.items() if k != '_queuedAt'} for e in events]
        result = self._try_urls(payload_events)
        if result['ok'] or result['delivered']:
            self._write_queue([])
            self._memory_queue = []
            return result['ok']
        # 写不进磁盘时退化为内存队列，保证本次进程内仍能重试（不静默丢事件）
        persisted = self._write_queue(events)
        if not persisted:
            self._memory_queue = events[-self.queue_limit:]
        self.log('warn', f"上报失败，已缓存 {len(events)} 条待补发"
                         f"（{'磁盘' if persisted else '内存'}）：{result.get('reason', '')}")
        return False

    def _append_event(self, event: Dict[str, Any]) -> None:
        '''入队一条事件（带入队时间，供过期清理使用）；磁盘不可写时改用内存队列'''
        queued = dict(event, _queuedAt=time.time() * 1000)
        if self._write_queue(self._read_queue() + [queued]):
            self._memory_queue = []
            return
        # 磁盘不可写时宁可放内存也不要丢，避免「以为成功其实没发」的静默事故
        self.log('warn', f'队列文件不可写：{self.queue_path}，本条事件改用内存队列')
        self._memory_queue = (self._memory_queue + [queued])[-self.queue_limit:]

    def _track_core(self, payload: Optional[Dict[str, Any]] = None) -> bool:
        '''发送主流程（调用方负责串行化）'''
        payload = payload or {}
        event = self._build_event(payload)
        key = self._fingerprint(event)
        if self.dedupe:
            state = self._read_state()
            if key in state['reported']:
                self.log('debug', f'已上报过，跳过：{key}')
                return True
            # 去重模式：先标记再发送（至多一次），宁可丢也不重复计数
            state['reported'][key] = time.time() * 1000
            _write_json(self.state_path, state)
        # 先落盘再发送（至少一次），进程被强杀也不会丢事件
        self._append_event(event)
        ok = self._send_once()
        return True if self.dedupe else ok

    def track(self, payload: Optional[Dict[str, Any]] = None) -> bool:
        '''上报一条事件（发后不管，立即返回，网络请求在后台线程进行；线程内串行执行）'''
        payload = payload or {}

        def _run():
            with self._send_lock:
                try:
                    self._track_core(payload)
                except Exception as err:
                    self.log('warn', f'track failed: {err}')

        threading.Thread(target=_run, name='st-track', daemon=True).start()
        return True

    def track_async(self, payload: Optional[Dict[str, Any]] = None) -> bool:
        '''上报一条事件并等待结果（卸载程序退出前使用）'''
        payload = payload or {}
        with self._send_lock:
            try:
                return self._track_core(payload)
            except Exception as err:
                self.log('warn', f'track_async failed: {err}')
                return False

    def flush_queue(self) -> None:
        '''仅补发历史失败队列，不产生新事件（队列为空时不做任何网络请求）'''
        def _run():
            with self._send_lock:
                try:
                    self._send_once()
                except Exception as err:
                    self.log('warn', f'flush failed: {err}')

        threading.Thread(target=_run, name='st-flush', daemon=True).start()

    def pending_count(self) -> int:
        '''当前积压的事件条数（含内存兜底队列），可用于自检/告警'''
        try:
            return len(self._read_queue()) + len(self._memory_queue)
        except Exception:
            return 0


def create_tracker(server_url: Optional[str] = None, app_key: Optional[str] = None,
                   version: str = '', channel: str = 'official', **kwargs) -> Tracker:
    '''创建上报实例

    serverUrl / appKey / 备用域名均可被环境变量覆盖：
        VD_TRACKER_SERVER / VD_TRACKER_APP_KEY / VD_TRACKER_FALLBACK_URLS（逗号分隔）
    '''
    server_url = server_url or os.environ.get('VD_TRACKER_SERVER') or DEFAULT_SERVER_URL
    app_key = app_key or os.environ.get('VD_TRACKER_APP_KEY') or DEFAULT_APP_KEY
    if 'fallback_urls' not in kwargs:
        env_fallback = os.environ.get('VD_TRACKER_FALLBACK_URLS')
        if env_fallback:
            kwargs['fallback_urls'] = [u.strip() for u in env_fallback.split(',') if u.strip()]
    return Tracker(server_url=server_url, app_key=app_key, version=version, channel=channel, **kwargs)
