'''
Function:
    In-app login window manager for the vd desktop shell.

    Opens a real (DrissionPage-driven) browser window so the user can sign in to
    a platform, then captures the full cookie set (including HttpOnly cookies that
    a WebView2 `document.cookie` read can never see) and stores it per-source so
    subsequent parses/downloads automatically carry the login session.

    Why DrissionPage instead of an in-WebView2 popup: Windows WebView2 (pywebview)
    cannot reliably read HttpOnly login cookies, so the captured session would be
    incomplete and the login effectively useless. DrissionPage is already a hard
    dependency of the vendored vd engine and exposes `getcookiesdict()` that
    returns the complete cookie jar.
Author:
    CodeBuddy
'''
from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

DESKTOP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DESKTOP_ROOT.parent
VD_SRC = PROJECT_ROOT / 'engine'
if str(VD_SRC) not in sys.path and VD_SRC.exists():
    sys.path.insert(0, str(VD_SRC))

# Map a vd source class name to its login / home page. Extend as needed.
# Unknown sources fall back to "manual cookie" in the settings UI.
LOGIN_URLS = {
    'BilibiliVideoClient': 'https://passport.bilibili.com/login',
    'YouTubeVideoClient': 'https://www.youtube.com/',
    'DouyinVideoClient': 'https://www.douyin.com/',
    'KuaishouVideoClient': 'https://www.kuaishou.com/',
    'WeiboVideoClient': 'https://weibo.com/login.php',
    'XiaohongshuVideoClient': 'https://www.xiaohongshu.com/explore',
    'TencentVideoClient': 'https://v.qq.com/',
    'IqiyiVideoClient': 'https://www.iqiyi.com/',
    'YoukuVideoClient': 'https://www.youku.com/',
    'BaiduTiebaVideoClient': 'https://tieba.baidu.com/',
    'XiguaVideoClient': 'https://www.ixigua.com/',
}


def source_login_url(source: str) -> Optional[str]:
    return LOGIN_URLS.get(source)


def _find_real_chrome_user_data() -> Optional[str]:
    '''Locate the user's *real* Chrome profile dir (where B站/Douyin are trusted).'''
    base = os.environ.get('LOCALAPPDATA')
    candidates = []
    if base:
        candidates.append(os.path.join(base, 'Google', 'Chrome', 'User Data'))
    candidates.append(os.path.expanduser('~/AppData/Local/Google/Chrome/User Data'))
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return None


def _chrome_profile_locked(user_data: str) -> bool:
    '''Chrome creates a SingletonLock in its User Data dir while running.'''
    return bool(user_data) and os.path.exists(os.path.join(user_data, 'SingletonLock'))


# Key login cookies that *must* be present for a platform session to count as
# "logged in". DrissionPage can silently capture an empty/incomplete jar (e.g.
# the user closed the window without finishing, or a popup/iframe set cookies
# on a different domain), so we verify before trusting the capture.
LOGIN_REQUIRED_COOKIES = {
    'YouTubeVideoClient': ('LOGIN_INFO', '__Secure-1PSID', 'SID'),
    'BilibiliVideoClient': ('SESSDATA', 'DedeUserID'),
    'DouyinVideoClient': ('sessionid', 'sid_tt', 'ttwid'),
    'KuaishouVideoClient': ('did', 'userId'),
    'WeiboVideoClient': ('SUB', 'SUBP'),
    'XiaohongshuVideoClient': ('a1', 'web_session'),
    'TencentVideoClient': ('vqq_vusession', 'vqq_access_token'),
    'IqiyiVideoClient': ('P00002', 'QC005'),
    'YoukuVideoClient': ('_savec5', 'cna'),
    'BaiduTiebaVideoClient': ('BDUSS',),
    'XiguaVideoClient': ('sessionid', 'sid_tt', 'ttwid'),
}


def _has_login_cookie(source: str, cookies: Dict[str, Any]) -> bool:
    '''Return True only if the captured jar contains a platform key login cookie.'''
    if not cookies:
        return False
    required = LOGIN_REQUIRED_COOKIES.get(source)
    if not required:
        return True
    return any(k in cookies for k in required)


def _cookie_dict_to_string(cookies: Dict[str, Any]) -> str:
    '''Flatten a DrissionPage cookie dict (values may be scalars or dicts).'''
    if not cookies:
        return ''
    parts: List[str] = []
    for key, val in cookies.items():
        if isinstance(val, dict):
            val = val.get('value', '')
        parts.append(f'{key}={val}')
    return '; '.join(parts)


class LoginManager():
    '''Drives one browser-based login at a time per source.'''

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def start(self, source: str, service) -> Dict[str, Any]:
        with self._lock:
            existing = self._tasks.get(source)
            if existing and existing.get('thread') and existing['thread'].is_alive() and existing.get('state') in ('opening', 'waiting'):
                return {'ok': True, 'state': existing['state']}
        url = source_login_url(source)
        if not url:
            return {'ok': False, 'error': f'暂不支持平台「{source}」的自动登录，请在设置中手动填写 Cookie'}
        event = threading.Event()
        task: Dict[str, Any] = {'state': 'opening', 'error': '', 'event': event, 'thread': None, 'source': source}
        # Prefer reusing the user's REAL Chrome profile so platforms treat the
        # session as a trusted, already-logged-in device (avoids the "unknown
        # browser" risk-control that caps quality at 720P). We can only safely
        # take over its User Data dir when Chrome is fully closed.
        real_ud = _find_real_chrome_user_data()
        use_ud = real_ud if (real_ud and not _chrome_profile_locked(real_ud)) else None
        task['user_data_path'] = use_ud
        if real_ud and not use_ud:
            task['hint'] = ('检测到 Chrome 正在运行，已改用独立环境登录（可能被 B站风控限制到 720P）。'
                            '请先关闭 Chrome，再重新点「登录」，即可解锁 1080P/4K。')
        with self._lock:
            self._tasks[source] = task

        def run() -> None:
            page = None
            try:
                from vd.modules.utils.chromium import DrissionPageUtils
                task['state'] = 'opening'
                page = DrissionPageUtils.initsmartbrowser(headless=False, user_data_path=task.get('user_data_path'))
                page.get(url)
                task['state'] = 'waiting'
                # Block until the user finishes the login in the browser and the
                # frontend calls `login_finish`, or a generous timeout elapses.
                if not event.wait(timeout=900):
                    task['state'] = 'timeout'
                    try:
                        page.quit()
                    except Exception:
                        pass
                    return
                task['state'] = 'extracting'
                cookies = DrissionPageUtils.getcookiesdict(page) or {}
                cookie_str = _cookie_dict_to_string(cookies)
                if not _has_login_cookie(source, cookies):
                    task['state'] = 'incomplete'
                    task['error'] = (
                        f'登录未完成：未能捕获到「{source}」的关键登录 Cookie。'
                        '请确认已在弹出的浏览器中真正登录账号（右上角头像/昵称可见），'
                        '再点"完成提取"。本次未覆盖已有的 Cookie。'
                    )
                    try:
                        page.quit()
                    except Exception:
                        pass
                    return
                # Assign a *new* dict (do not mutate in place) so any consumer that
                # caches a reference (e.g. the VideoClient build signature) sees a
                # changed value and rebuilds with the freshly captured login cookies.
                updated = dict(service.config.per_source_cookies or {})
                updated[source] = cookie_str
                service.config.per_source_cookies = updated
                service.config.save()
                task['state'] = 'done'
                try:
                    page.quit()
                except Exception:
                    pass
            except Exception as err:
                task['state'] = 'error'
                task['error'] = f'{err}'
                try:
                    if page is not None:
                        page.quit()
                except Exception:
                    pass

        thread = threading.Thread(target=run, name=f'login-{source}', daemon=True)
        task['thread'] = thread
        thread.start()
        return {'ok': True, 'state': 'opening', 'hint': task.get('hint', '')}

    def finish(self, source: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(source)
            if not task or task.get('state') not in ('opening', 'waiting'):
                return {'ok': False, 'error': '没有进行中的登录任务'}
            task['event'].set()
        return {'ok': True}

    def status(self, source: Optional[str] = None) -> Any:
        with self._lock:
            if source:
                task = self._tasks.get(source)
                if not task:
                    return {'source': source, 'state': 'absent'}
                return {'source': source, 'state': task.get('state', 'absent'), 'error': task.get('error', '')}
            return [{'source': s, 'state': t.get('state', 'absent'), 'error': t.get('error', '')} for s, t in self._tasks.items()]

    def logout(self, source: str, service) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(source)
            if task and task.get('event'):
                task['event'].set()
            self._tasks.pop(source, None)
        cookies = dict(service.config.per_source_cookies or {})
        if source in cookies:
            del cookies[source]
            service.config.per_source_cookies = cookies
            service.config.save()
        return {'ok': True}


login_manager = LoginManager()
