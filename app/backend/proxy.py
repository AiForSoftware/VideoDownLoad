'''
Function:
    System proxy detection (Windows: IE/WinHTTP settings incl. PAC; others: env vars)
Author:
    CodeBuddy
'''
from __future__ import annotations

import os
import time
import urllib.request
from typing import Dict, Optional
from urllib.parse import urlsplit

# 探测结果缓存：PAC 脚本可能很慢（甚至每次都要联网取脚本），
# 但解析一次的结果在几分钟内是稳定的，按 (配置指纹, 目标 host) 缓存。
_CACHE: Dict[tuple, tuple] = {}
_CACHE_TTL = 120.0
_DEFAULT_TARGET = 'https://www.youtube.com/'


def _normalizeproxy(value: str) -> str:
    '''把 "host:port" / "http://host:port" 统一成带 scheme 的形式（requests 需要）。'''
    value = (value or '').strip().strip(';').strip()
    if not value:
        return ''
    if '://' not in value:
        value = 'http://' + value
    return value


def _parseproxystring(raw: str) -> Dict[str, str]:
    '''解析 WinHTTP 返回的代理串：
       "192.168.1.1:1080"（所有协议同一代理）或
       "http=192.168.1.1:1080;https=192.168.1.1:1080;socks=..."'''
    raw = (raw or '').strip()
    if not raw:
        return {}
    result: Dict[str, str] = {}
    if '=' not in raw:
        proxy = _normalizeproxy(raw)
        return {'http': proxy, 'https': proxy} if proxy else {}
    for part in raw.split(';'):
        part = part.strip()
        if '=' not in part:
            continue
        scheme, addr = part.split('=', 1)
        scheme = scheme.strip().lower()
        proxy = _normalizeproxy(addr)
        if not proxy:
            continue
        if scheme in ('http', 'https'):
            result[scheme] = proxy
        elif scheme.startswith('socks'):
            # requests 需要额外的 socks 支持（PySocks），本机不一定装；
            # 交给上层忽略，避免整条链路因为不支持的协议而失败。
            continue
    if result and 'https' not in result and 'http' in result:
        result['https'] = result['http']
    if result and 'http' not in result and 'https' in result:
        result['http'] = result['https']
    return result


def _fromenvironment() -> Dict[str, str]:
    '''环境变量（HTTP_PROXY / HTTPS_PROXY / ALL_PROXY）优先——这是显式覆盖。'''
    env = urllib.request.getproxies_environment()
    if not env:
        return {}
    return {k: _normalizeproxy(v) for k, v in env.items() if k in ('http', 'https')}


def _winhttp_systemproxy(target_url: str) -> Dict[str, str]:
    '''Windows：走 WinHTTP 读当前用户的 IE/系统代理设置。
    ★ 关键：系统的"自动配置脚本"（PAC）只有 WinHTTP 能执行——注册表里
      只有 AutoConfigURL 一行 URL，urllib.request.getproxies() 读注册表
      拿到的是空字典，这就是为什么以前"配了系统代理但软件不走"。'''
    import ctypes
    from ctypes import wintypes

    winhttp = ctypes.WinDLL('winhttp', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    class WINHTTP_CURRENT_USER_IE_PROXY_CONFIG(ctypes.Structure):
        _fields_ = [('fAutoDetect', wintypes.BOOL),
                    ('lpszAutoConfigUrl', wintypes.LPWSTR),
                    ('lpszProxy', wintypes.LPWSTR),
                    ('lpszProxyBypass', wintypes.LPWSTR)]

    class WINHTTP_AUTOPROXY_OPTIONS(ctypes.Structure):
        _fields_ = [('dwFlags', wintypes.DWORD),
                    ('dwAutoDetectFlags', wintypes.DWORD),
                    ('lpszAutoConfigUrl', wintypes.LPCWSTR),
                    ('lpvReserved', ctypes.c_void_p),
                    ('dwReserved', wintypes.DWORD),
                    ('fAutoLogonIfChallenged', wintypes.BOOL)]

    class WINHTTP_PROXY_INFO(ctypes.Structure):
        _fields_ = [('dwAccessType', wintypes.DWORD),
                    ('lpszProxy', wintypes.LPWSTR),
                    ('lpszProxyBypass', wintypes.LPWSTR)]

    winhttp.WinHttpGetIEProxyConfigForCurrentUser.argtypes = [ctypes.POINTER(WINHTTP_CURRENT_USER_IE_PROXY_CONFIG)]
    winhttp.WinHttpGetIEProxyConfigForCurrentUser.restype = wintypes.BOOL
    winhttp.WinHttpOpen.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    winhttp.WinHttpOpen.restype = wintypes.HANDLE
    winhttp.WinHttpGetProxyForUrl.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR,
                                              ctypes.POINTER(WINHTTP_AUTOPROXY_OPTIONS),
                                              ctypes.POINTER(WINHTTP_PROXY_INFO)]
    winhttp.WinHttpGetProxyForUrl.restype = wintypes.BOOL
    winhttp.WinHttpCloseHandle.argtypes = [wintypes.HANDLE]
    winhttp.WinHttpCloseHandle.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    cfg = WINHTTP_CURRENT_USER_IE_PROXY_CONFIG()
    if not winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(cfg)):
        return {}
    pac_url = cfg.lpszAutoConfigUrl or ''
    static_proxy = cfg.lpszProxy or ''
    bypass = cfg.lpszProxyBypass or ''
    autodetect = bool(cfg.fAutoDetect)
    # 静态代理（没有 PAC）直接解析，不需要跑 PAC 引擎
    if static_proxy and not pac_url:
        result = _parseproxystring(static_proxy)
        _free(kernel32, cfg)
        return {} if _isbypassed(bypass, target_url) else result
    if not pac_url and not autodetect:
        _free(kernel32, cfg)
        return {}

    session = winhttp.WinHttpOpen('vd-desktop', 0, None, None, 0)
    if not session:
        _free(kernel32, cfg)
        return {}
    try:
        opts = WINHTTP_AUTOPROXY_OPTIONS()
        if pac_url:
            opts.dwFlags = 0x00000002  # WINHTTP_AUTOPROXY_CONFIG_URL
            opts.lpszAutoConfigUrl = pac_url
        else:
            opts.dwFlags = 0x00000001  # WINHTTP_AUTOPROXY_AUTO_DETECT
            opts.dwAutoDetectFlags = 0x00000002 | 0x00000004  # DHCP | DNS
        opts.fAutoLogonIfChallenged = True
        info = WINHTTP_PROXY_INFO()
        if not winhttp.WinHttpGetProxyForUrl(session, target_url, ctypes.byref(opts), ctypes.byref(info)):
            return {}
        result = _parseproxystring(info.lpszProxy or '')
        if info.lpszProxyBypass and _isbypassed(info.lpszProxyBypass, target_url):
            result = {}
        return result
    finally:
        winhttp.WinHttpCloseHandle(session)
        _free(kernel32, cfg)


def _free(kernel32, struct) -> None:
    '''释放 WinHTTP 分配的字符串（不释放会持续泄漏内存）。'''
    for field in ('lpszAutoConfigUrl', 'lpszProxy', 'lpszProxyBypass'):
        ptr = getattr(struct, field, None)
        if ptr:
            try:
                kernel32.GlobalFree(ctypes.cast(ptr, ctypes.c_void_p))
            except Exception:
                pass
        try:
            setattr(struct, field, None)
        except Exception:
            pass


def _isbypassed(bypass: str, target_url: str) -> bool:
    '''极简 bypass 判断（只处理 '*' 与主机名包含匹配，覆盖绝大多数配置）。'''
    bypass = (bypass or '').strip()
    if not bypass:
        return False
    if bypass == '*' or '<-loopback>' in bypass:
        return bypass == '*'
    host = (urlsplit(target_url).hostname or '').lower()
    for entry in bypass.replace(';', ' ').split():
        entry = entry.strip().lower()
        if not entry or entry == '<local>':
            continue
        if entry.startswith('*.'):
            if host.endswith(entry[1:]):
                return True
        elif host == entry or host.endswith('.' + entry):
            return True
    return False


def systemproxy(target_url: str = None) -> Dict[str, str]:
    '''返回 requests 形式的代理字典（{'http': ..., 'https': ...}），没有则 {}。'''
    target_url = target_url or _DEFAULT_TARGET
    try:
        host = urlsplit(target_url).hostname or ''
    except Exception:
        host = ''
    cache_key = ('proxy', host)
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return dict(cached[0])
    proxies: Dict[str, str] = {}
    try:
        proxies = _fromenvironment()
        if not proxies and os.name == 'nt':
            proxies = _winhttp_systemproxy(target_url)
    except Exception:
        proxies = {}
    _CACHE[cache_key] = (dict(proxies), now)
    return dict(proxies)


def describe(proxies: Optional[Dict[str, str]] = None) -> str:
    '''给日志/UI 用的一句话描述。'''
    proxies = proxies if proxies is not None else systemproxy()
    if not proxies:
        return '未检测到系统代理（直连）'
    return ', '.join(f'{k}={v}' for k, v in sorted(proxies.items()))
