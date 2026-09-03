'''
Function:
    Implementation of Chromium Related Utils
'''
import os
import sys
import stat
import time
import socket
import shutil
import zipfile
import platform
import tempfile
import requests
from uuid import uuid4
from .io import FileLock
from pathlib import Path
from urllib.parse import urlsplit
from .importutils import optionalimportfrom
from typing import Optional, Tuple, Callable, TYPE_CHECKING


'''ChromiumDownloaderUtils'''
class ChromiumDownloaderUtils():
    '''defaulttargetdir'''
    @staticmethod
    def defaulttargetdir() -> Path:
        if "__file__" in globals(): return Path(__file__).resolve().parent / "chrome_bin"
        return Path.cwd() / "chrome_bin"
    '''resolveplatform'''
    @staticmethod
    def resolveplatform() -> Tuple[str, Path]:
        sys_plat = sys.platform; machine = platform.machine().lower()
        if sys_plat == "win32": return ("win64", Path("chrome.exe")) if machine in ("amd64", "x86_64") else ("win32", Path("chrome.exe")) if machine in ("x86", "i386", "i686") else (_ for _ in ()).throw(RuntimeError(f"Unsupported Windows architecture for Chrome for Testing: {machine}"))
        if sys_plat == "darwin": return ("mac-arm64", Path("Google Chrome for Testing.app") / "Contents" / "MacOS" / "Google Chrome for Testing") if machine == "arm64" else ("mac-x64", Path("Google Chrome for Testing.app") / "Contents" / "MacOS" / "Google Chrome for Testing") if machine in ("x86_64", "amd64") else (_ for _ in ()).throw(RuntimeError(f"Unsupported macOS architecture for Chrome for Testing: {machine}"))
        if sys_plat.startswith("linux"): return ("linux64", Path("chrome")) if machine in ("x86_64", "amd64") else (_ for _ in ()).throw(RuntimeError(f"Unsupported Linux architecture for Chrome for Testing: {machine}"))
        raise RuntimeError(f"Unsupported platform: {sys_plat}")
    '''makeexecutable'''
    @staticmethod
    def makeexecutable(path: Path) -> None:
        if os.name == "nt": return
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    '''safeextract'''
    @staticmethod
    def safeextract(zip_ref: zipfile.ZipFile, target_dir: Path) -> None:
        target_dir = target_dir.resolve()
        for member in zip_ref.infolist():
            member_path = (target_dir / member.filename).resolve()
            if not str(member_path).startswith(str(target_dir)): raise RuntimeError(f"Unsafe zip member detected: {member.filename}")
        zip_ref.extractall(target_dir)
    '''removetree'''
    @staticmethod
    def removetree(path: Path) -> None:
        if path.exists(): shutil.rmtree(path, ignore_errors=True)
    '''requestjsonwithretry'''
    @staticmethod
    def requestjsonwithretry(session: requests.Session, url: str, retries: int = 3) -> dict:
        last_exc = None
        for attempt in range(1, retries + 1):
            try: (resp := session.get(url, timeout=(10, 30))).raise_for_status(); return resp.json()
            except Exception as err:
                last_exc = err
                if attempt < retries: time.sleep(1.0 * attempt)
        raise RuntimeError(f"Failed to fetch JSON from {url}: {last_exc}") from last_exc
    '''getstabledownloadinfo'''
    @staticmethod
    def getstabledownloadinfo(session: requests.Session, pf: str, channel: str = "Stable") -> Tuple[str, str]:
        api_url = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
        data = ChromiumDownloaderUtils.requestjsonwithretry(session, api_url)
        try: version = (channel_info := data["channels"][channel])["version"]; downloads = channel_info["downloads"]["chrome"]; download_url = next(item["url"] for item in downloads if item["platform"] == pf); return version, download_url
        except Exception as e: raise RuntimeError(f"Failed to parse Chrome download info for channel={channel}, platform={pf}") from e
    '''downloadfilewithretry'''
    @staticmethod
    def downloadfilewithretry(session: requests.Session, url: str, dst_path: Path, retries: int = 3, chunk_size: int = 1024 * 1024) -> None:
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                with session.get(url, stream=True, timeout=(10, 180)) as resp:
                    resp.raise_for_status()
                    with open(dst_path, "wb") as fp:
                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if chunk: fp.write(chunk)
                if dst_path.stat().st_size == 0: raise RuntimeError("Downloaded file is empty.")
                return
            except Exception as err:
                last_exc = err
                try:
                    if dst_path.exists(): dst_path.unlink()
                except Exception:
                    pass
                if attempt < retries: time.sleep(1.5 * attempt)
        raise RuntimeError(f"Failed to download file from {url}: {last_exc}") from last_exc
    '''cleanupoldversions'''
    @staticmethod
    def cleanupoldversions(base_platform_dir: Path, keep_version: str) -> None:
        if not base_platform_dir.exists(): return
        for child in base_platform_dir.iterdir():
            if not child.is_dir(): continue
            if (name := child.name) == keep_version: continue
            if name.startswith(".tmp-"): ChromiumDownloaderUtils.removetree(child); continue
            ChromiumDownloaderUtils.removetree(child)
    '''autodownloadchrome'''
    @staticmethod
    def autodownloadchrome(target_dir: Optional[str] = None, channel: str = "Stable", force_redownload: bool = False, cleanup_old_versions: bool = False, lock_timeout: float = 300.0) -> str:
        base_dir = Path(target_dir).resolve() if target_dir else ChromiumDownloaderUtils.defaulttargetdir().resolve()
        base_dir.mkdir(parents=True, exist_ok=True); pf, exe_inside_package = ChromiumDownloaderUtils.resolveplatform()
        with requests.Session() as session:
            session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; autodownloadchrome/1.0)"})
            version, download_url = ChromiumDownloaderUtils.getstabledownloadinfo(session, pf, channel=channel)
            platform_dir = base_dir / pf; version_dir = platform_dir / version; final_exe = version_dir / f"chrome-{pf}" / exe_inside_package; lock_path = platform_dir / ".install.lock"
            if final_exe.is_file() and not force_redownload: os.name != "nt" and ChromiumDownloaderUtils.makeexecutable(final_exe); return str(final_exe)
            with FileLock(lock_path, timeout=lock_timeout):
                if final_exe.is_file() and not force_redownload: os.name != "nt" and ChromiumDownloaderUtils.makeexecutable(final_exe); return str(final_exe)
                if force_redownload and version_dir.exists(): ChromiumDownloaderUtils.removetree(version_dir)
                tmp_dir = platform_dir / f".tmp-{version}-{uuid4().hex}"; extract_dir = tmp_dir / "extract"; zip_path = tmp_dir / "chrome.zip"
                try:
                    tmp_dir.mkdir(parents=True, exist_ok=False); extract_dir.mkdir(parents=True, exist_ok=False)
                    ChromiumDownloaderUtils.downloadfilewithretry(session, download_url, zip_path)
                    try:
                        with zipfile.ZipFile(zip_path, "r") as zip_ref: ChromiumDownloaderUtils.safeextract(zip_ref, extract_dir)
                    except zipfile.BadZipFile as err: raise RuntimeError(f"Downloaded file is not a valid zip archive: {zip_path}") from err
                    if not (extracted_exe := extract_dir / f"chrome-{pf}" / exe_inside_package).is_file(): raise FileNotFoundError(f"Chrome executable not found after extraction: {extracted_exe}")
                    if os.name != "nt": ChromiumDownloaderUtils.makeexecutable(extracted_exe)
                    if version_dir.exists(): ChromiumDownloaderUtils.removetree(version_dir)
                    os.replace(str(extract_dir), str(version_dir))
                    if not final_exe.is_file(): raise FileNotFoundError(f"Chrome executable missing after install: {final_exe}")
                    if os.name != "nt": ChromiumDownloaderUtils.makeexecutable(final_exe)
                    if cleanup_old_versions: ChromiumDownloaderUtils.cleanupoldversions(platform_dir, keep_version=version)
                    return str(final_exe)
                finally:
                    ChromiumDownloaderUtils.removetree(tmp_dir)


'''DrissionPageUtils'''
class DrissionPageUtils():
    ChromiumPage = optionalimportfrom("DrissionPage", "ChromiumPage")
    ChromiumOptions = optionalimportfrom("DrissionPage", "ChromiumOptions")
    if TYPE_CHECKING: from DrissionPage import ChromiumPage as ChromiumPage, ChromiumOptions as ChromiumOptions
    '''islinux'''
    @staticmethod
    def islinux() -> bool:
        return sys.platform.startswith("linux")
    '''isci'''
    @staticmethod
    def isci() -> bool:
        return os.getenv("CI", "").lower() == "true"
    '''isgithubactions'''
    @staticmethod
    def isgithubactions() -> bool:
        return os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    '''isroot'''
    @staticmethod
    def isroot() -> bool:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    '''getdevshmsizemb'''
    @staticmethod
    def getdevshmsizemb() -> float:
        if not DrissionPageUtils.islinux(): return 1024.0
        try: statvfs = os.statvfs("/dev/shm"); return statvfs.f_frsize * statvfs.f_blocks / (1024 * 1024)
        except Exception: return 0.0
    '''needdisabledevshm'''
    @staticmethod
    def needdisabledevshm() -> bool:
        if not DrissionPageUtils.islinux(): return False
        try: return (True if ((not os.path.exists((shm_path := "/dev/shm"))) or (not os.access(shm_path, os.W_OK | os.X_OK)) or (DrissionPageUtils.getdevshmsizemb() < 128)) else False)
        except Exception: return True
    '''safesetargument'''
    @staticmethod
    def safesetargument(co, argument: str):
        if TYPE_CHECKING: from DrissionPage import ChromiumOptions; assert isinstance(co, ChromiumOptions)
        try: co.set_argument(argument)
        except Exception: pass
        return co
    '''safeautoport'''
    @staticmethod
    def safeautoport(co):
        if TYPE_CHECKING: from DrissionPage import ChromiumOptions; assert isinstance(co, ChromiumOptions)
        try: co.auto_port(); return True
        except Exception: return False
    '''applyenvironmentarguments'''
    @staticmethod
    def applyenvironmentarguments(co, headless: bool = True):
        is_linux, is_ci, is_root = DrissionPageUtils.islinux(), DrissionPageUtils.isci() or DrissionPageUtils.isgithubactions(), DrissionPageUtils.isroot()
        if headless and is_linux: DrissionPageUtils.safesetargument(co, "--headless=new")
        if is_linux and (is_ci or is_root): DrissionPageUtils.safesetargument(co, "--no-sandbox")
        if DrissionPageUtils.needdisabledevshm(): DrissionPageUtils.safesetargument(co, "--disable-dev-shm-usage")
        if headless: DrissionPageUtils.safesetargument(co, "--disable-gpu")
        DrissionPageUtils.safesetargument(co, "--window-size=1920,1080")
        return co
    '''pickportforpagelaunch'''
    @staticmethod
    def pickportforpagelaunch(co, host='127.0.0.1', retries=5, delay=0.05):
        if TYPE_CHECKING: from DrissionPage import ChromiumOptions; assert isinstance(co, ChromiumOptions)
        for _ in range(retries):
            (sock := socket.socket(socket.AF_INET, socket.SOCK_STREAM)).bind((host, 0))
            port = sock.getsockname()[1]
            try: co.set_local_port(port); sock.close(); page = DrissionPageUtils.ChromiumPage(co); return port, page
            except Exception:
                try: sock.close(); time.sleep(delay)
                except Exception: pass
        raise RuntimeError('Fail to pick port for DrissionPage launch')
    '''buildoptions'''
    @staticmethod
    def buildoptions(headless: bool = True, browser_path: str = None, user_data_path: str = None):
        co = DrissionPageUtils.ChromiumOptions()
        if headless:
            # MUST go through co.headless() so DrissionPage's internal `is_headless`
            # flag matches the real browser. Injecting '--headless=new' via
            # set_argument() leaves is_headless=False while the launched browser IS
            # headless; Chromium.__init__ then treats that as a state mismatch,
            # quits the freshly launched browser, relaunches it and reconnects with
            # the DEAD browser's GUID — DevTools answers "Handshake status 404"
            # (WebSocketBadStatusException, seen on Edge 152). On DrissionPage >= 4.1
            # co.headless(True) already emits the modern --headless=new flag.
            try:
                co.headless(True)
            except Exception:
                DrissionPageUtils.safesetargument(co, '--headless=new')
        if browser_path: co.set_browser_path(browser_path)
        if user_data_path:
            try: co.set_user_data_path(user_data_path)
            except Exception: pass
        DrissionPageUtils.applyenvironmentarguments(co, headless=headless)
        return co
    '''killdebugbrowsers'''
    @staticmethod
    def killdebugbrowsers(address: str = ''):
        '''Best-effort kill of leftover headless browser processes launched on
        `address` (host:port). ChromiumPage() can fail AFTER the browser process
        already started (e.g. a failed WebSocket handshake); without this those
        orphans accumulate (each holding a full browser in RAM). Only processes
        whose command line carries OUR --remote-debugging-port are touched, so
        the user's normal browser is never affected.'''
        try:
            port = str(address or '').split(':')[-1]
        except Exception:
            return 0
        if not port or not port.isdigit():
            return 0
        killed = 0
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    name = (proc.info.get('name') or '').lower()
                    if name not in ('msedge.exe', 'chrome.exe', 'chromium.exe', 'msedge', 'chrome'):
                        continue
                    cmdline = proc.info.get('cmdline') or []
                    if any(str(a).startswith(f'--remote-debugging-port={port}') for a in cmdline):
                        proc.kill()
                        killed += 1
                except Exception:
                    continue
        except Exception:
            pass
        if killed:
            DrissionPageUtils.clearcache()
        return killed
    '''trylaunchbrowser'''
    @staticmethod
    def trylaunchbrowser(headless: bool = True, apply_auto_port: bool = True, browser_path: str = None, user_data_path: str = None, requests_headers: dict = None, requests_proxies: dict = None, requests_cookies: dict = None, requests_cookies_domain: str = None, co_hook_func: Callable = None, co_hook_func_args: dict = None, page_hook_func: Callable = None, page_hook_func_args: dict = None):
        requests_headers, requests_proxies, requests_cookies, co_hook_func_args, page_hook_func_args = dict(requests_headers or {}), dict(requests_proxies or {}), dict(requests_cookies or {}), dict(co_hook_func_args or {}), dict(page_hook_func_args or {})
        co = DrissionPageUtils.buildoptions(headless=headless, browser_path=browser_path, user_data_path=user_data_path)
        if (proxy_str := DrissionPageUtils.requestsproxytodrissionpage(requests_proxies, mode="chromium", strip_auth_for_chromium=False)): co.set_proxy(proxy_str)
        if (user_agent := requests_headers.pop('User-Agent', None) or requests_headers.pop('user-agent', None)): co.set_user_agent(user_agent=user_agent)
        if co_hook_func and callable(co_hook_func): co = co_hook_func(co, **co_hook_func_args)
        if apply_auto_port: DrissionPageUtils.safeautoport(co)
        try: page = DrissionPageUtils.ChromiumPage(co)
        except Exception as err:
            # the browser process may already be running — don't leak it
            DrissionPageUtils.killdebugbrowsers(str(co.address))
            env_info = {"platform": sys.platform, "machine": platform.machine(), "is_ci": DrissionPageUtils.isci(), "is_github_actions": DrissionPageUtils.isgithubactions(), "is_root": DrissionPageUtils.isroot(), "dev_shm_mb": DrissionPageUtils.getdevshmsizemb() if DrissionPageUtils.islinux() else None, "browser_path": browser_path, "headless": headless}; raise RuntimeError(f"Failed to launch ChromiumPage, env_info={env_info}, error={repr(err)}") from err
        if requests_headers: page.set.headers(requests_headers)
        if requests_cookies:
            # DrissionPage refuses cookies without a domain ("No domain name is set,
            # please set the domain parameter of the cookie or visit a website first"):
            # at this point the page is still on about:blank, so a flat {name: value}
            # dict cannot be scoped. Callers that know the target site pass
            # `requests_cookies_domain` and we build proper cookie dicts for it.
            is_flat = isinstance(requests_cookies, dict) and requests_cookies and all(isinstance(v, str) for v in requests_cookies.values())
            if is_flat and requests_cookies_domain:
                page.set.cookies([{'name': k, 'value': v, 'domain': requests_cookies_domain} for k, v in requests_cookies.items()])
            else:
                page.set.cookies(requests_cookies)
        if page_hook_func and callable(page_hook_func): page = page_hook_func(page, **page_hook_func_args)
        return page
    '''isvalidbrowserpath'''
    @staticmethod
    def isvalidbrowserpath(path: str) -> bool:
        if not path: return False
        return (p := Path(path)).exists() and p.is_file()
    '''parseversion'''
    @staticmethod
    def parseversion(version_str: str) -> Tuple[int, ...]:
        try: return tuple(int(x) for x in version_str.split("."))
        except Exception: return (0,)
    '''getlocalembeddedchrome'''
    @staticmethod
    def getlocalembeddedchrome(target_dir: Optional[str] = None) -> Optional[str]:
        base_dir = Path(target_dir).resolve() if target_dir else ChromiumDownloaderUtils.defaulttargetdir().resolve()
        pf, exe_inside_package = ChromiumDownloaderUtils.resolveplatform(); platform_dir = base_dir / pf; candidates = []
        if not platform_dir.exists(): return None
        for child in platform_dir.iterdir():
            if not child.is_dir() or child.name.startswith(".tmp-"): continue
            exe_path = child / f"chrome-{pf}" / exe_inside_package
            if DrissionPageUtils.isvalidbrowserpath(str(exe_path)): candidates.append((DrissionPageUtils.parseversion(child.name), exe_path))
        if not candidates: return None
        candidates.sort(key=lambda x: x[0], reverse=True); best_path = candidates[0][1]
        if os.name != "nt": ChromiumDownloaderUtils.makeexecutable(best_path)
        return str(best_path)
    '''findsystembrowser'''
    @staticmethod
    def findsystembrowser() -> Optional[str]:
        '''Return the path of a system-installed Chromium-based browser (Edge/Chrome).
        Used so callers can avoid DrissionPage's automatic Chrome-for-Testing download,
        which is unreachable / extremely slow behind the GFW.'''
        import shutil, winreg
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for c in candidates:
            if os.path.exists(c): return c
        for name in ("msedge.exe", "chrome.exe"):
            if (p := shutil.which(name)): return p
        for hk in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                        r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"):
                try:
                    with winreg.OpenKey(hk, sub) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        if val and os.path.exists(val): return val
                except Exception: pass
        return None
    '''initsmartbrowser'''
    @staticmethod
    def initsmartbrowser(headless: bool = True, apply_auto_port: bool = True, target_dir: Optional[str] = None, channel: str = "Stable", user_data_path: str = None, requests_headers: dict = None, requests_proxies: dict = None, requests_cookies: dict = None, requests_cookies_domain: str = None, co_hook_func: Callable = None, co_hook_func_args: dict = None, page_hook_func: Callable = None, page_hook_func_args: dict = None, browser_path: Optional[str] = None, allow_download: bool = True):
        errors, requests_headers, requests_proxies, requests_cookies = [], requests_headers or {}, requests_proxies or {}, requests_cookies or {}; DrissionPageUtils.clearcache()
        if browser_path:
            try: return DrissionPageUtils.trylaunchbrowser(headless=headless, apply_auto_port=apply_auto_port, browser_path=browser_path, user_data_path=user_data_path, requests_headers=requests_headers, requests_proxies=requests_proxies, requests_cookies=requests_cookies, requests_cookies_domain=requests_cookies_domain, co_hook_func=co_hook_func, co_hook_func_args=co_hook_func_args, page_hook_func=page_hook_func, page_hook_func_args=page_hook_func_args)
            except Exception as err: errors.append(f"[browser_path] {repr(err)}")
        else:
            try: return DrissionPageUtils.trylaunchbrowser(headless=headless, apply_auto_port=apply_auto_port, user_data_path=user_data_path, requests_headers=requests_headers, requests_proxies=requests_proxies, requests_cookies=requests_cookies, requests_cookies_domain=requests_cookies_domain, co_hook_func=co_hook_func, co_hook_func_args=co_hook_func_args, page_hook_func=page_hook_func, page_hook_func_args=page_hook_func_args)
            except Exception as err: errors.append(f"[system] {repr(err)}")
        local_chrome = DrissionPageUtils.getlocalembeddedchrome(target_dir=target_dir)
        if local_chrome:
            try: return DrissionPageUtils.trylaunchbrowser(headless=headless, browser_path=local_chrome, apply_auto_port=apply_auto_port, user_data_path=user_data_path, requests_headers=requests_headers, requests_proxies=requests_proxies, requests_cookies=requests_cookies, requests_cookies_domain=requests_cookies_domain, co_hook_func=co_hook_func, co_hook_func_args=co_hook_func_args, page_hook_func=page_hook_func, page_hook_func_args=page_hook_func_args)
            except Exception as err: errors.append(f"[local] {repr(err)}")
        if allow_download:
            try:
                chrome_path = ChromiumDownloaderUtils.autodownloadchrome(target_dir=target_dir, channel=channel)
                if not DrissionPageUtils.isvalidbrowserpath(chrome_path): raise RuntimeError(f"Invalid Chrome Path: {chrome_path}")
                return DrissionPageUtils.trylaunchbrowser(headless=headless, browser_path=chrome_path, apply_auto_port=apply_auto_port, user_data_path=user_data_path, requests_headers=requests_headers, requests_proxies=requests_proxies, requests_cookies=requests_cookies, requests_cookies_domain=requests_cookies_domain, co_hook_func=co_hook_func, co_hook_func_args=co_hook_func_args, page_hook_func=page_hook_func, page_hook_func_args=page_hook_func_args)
            except Exception as err: errors.append(f"[download] {repr(err)}")
        else:
            errors.append("[download] skipped (allow_download=False)")
        raise RuntimeError("\n".join(errors))
    '''getcookiesdict'''
    @staticmethod
    def getcookiesdict(page):
        if TYPE_CHECKING: from DrissionPage import ChromiumPage; assert isinstance(page, ChromiumPage)
        try: return page.cookies().as_dict()
        except TypeError: return page.cookies(as_dict=True)
    '''quitpage'''
    @staticmethod
    def quitpage(page):
        if TYPE_CHECKING: from DrissionPage import ChromiumPage; assert isinstance(page, ChromiumPage)
        try: page.quit()
        finally: DrissionPageUtils.clearcache()
    '''clearcache'''
    @staticmethod
    def clearcache():
        dp_temp_dir = os.path.join(tempfile.gettempdir(), 'DrissionPage', 'autoPortData')
        if os.path.exists(dp_temp_dir): shutil.rmtree(dp_temp_dir, ignore_errors=True)
    '''requestsproxytodrissionpage'''
    @staticmethod
    def requestsproxytodrissionpage(proxy: dict | str, *, prefer: tuple = ("https", "http", "all"), mode: str = "session", strip_auth_for_chromium: bool = False):
        def normalizeone(s: str) -> str | None:
            if (not s) or (not isinstance(s, str)) or (not (s := s.strip())): return None
            if "://" not in s: s = "http://" + s
            scheme = "socks5" if (u := urlsplit(s)).scheme == "socks5h" else u.scheme
            if not (host := u.hostname) or (port := u.port) is None: raise ValueError(f"Invalid proxy (need host:port): {s!r}")
            host, auth = f"[{host}]" if ":" in host else host, ""
            if u.username: auth = f"{u.username}:{u.password}@" if u.password is not None else f"{u.username}@"
            return f"{scheme}://{auth}{host}:{port}"
        if not proxy: return None
        if mode == "chromium":
            s = proxy if isinstance(proxy, str) else (next((proxy.get(k) for k in prefer if proxy.get(k)), None) or next(iter(proxy.values()), None))
            if not (s := normalizeone(s)): return None
            if (u := urlsplit(s)).username or u.password:
                if not strip_auth_for_chromium: raise ValueError("DrissionPage ChromiumOptions.set_proxy() does not support proxy auth (username/password).")
                host = f"[{u.hostname}]" if ":" in (u.hostname or "") else u.hostname
                return f"{u.scheme}://{host}:{u.port}"
            return s
        if mode == "session":
            if isinstance(proxy, str): s = normalizeone(proxy); return {"http": s, "https": s} if s else None
            if not isinstance(proxy, dict): return None
            out = {}
            if proxy.get("http"): out["http"] = normalizeone(proxy["http"])
            if proxy.get("https"): out["https"] = normalizeone(proxy["https"])
            if (all_proxy := proxy.get("all")): all_proxy = normalizeone(all_proxy); out.setdefault("http", all_proxy); out.setdefault("https", all_proxy)
            if not out:
                s = normalizeone(s) if (s := next((proxy.get(k) for k in prefer if proxy.get(k)), None)) else None
                if s: out = {"http": s, "https": s}
            return out or None
        raise ValueError("mode must be 'session' or 'chromium'")