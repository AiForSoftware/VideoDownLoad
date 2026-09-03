'''
Function:
    GUI smoke check: create a real pywebview window, call the js bridge from javascript,
    then close the window. It verifies that the webview shell works before packaging.
Author:
    CodeBuddy
'''
from __future__ import annotations

import sys
import time
import threading
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

DESKTOP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DESKTOP_ROOT))

import webview  # noqa: E402
from backend.api import JsApi  # noqa: E402

RESULT = {'bridge': None, 'bootstrap': None, 'errors': []}


def main() -> int:
    api = JsApi(version='gui-check')
    index = DESKTOP_ROOT / 'web' / 'index.html'
    window = webview.create_window('VideoDL GUI Check', index.as_uri(), js_api=api, width=1200, height=780)
    api.bindwindow(window)

    def probe():
        time.sleep(4)
        try:
            RESULT['bridge'] = window.evaluate_js("(window.pywebview && window.pywebview.api) ? 'ok' : 'missing'")
        except Exception as err:
            RESULT['errors'].append(f'bridge probe: {err}')
        time.sleep(1)
        try:
            window.evaluate_js("window.__r = 'pending'; window.pywebview.api.bootstrap().then(function(r){ window.__r = JSON.stringify(r); }).catch(function(e){ window.__r = 'ERR: ' + e; });")
        except Exception as err:
            RESULT['errors'].append(f'bootstrap call: {err}')
        time.sleep(3)
        try:
            RESULT['bootstrap'] = window.evaluate_js('window.__r')
        except Exception as err:
            RESULT['errors'].append(f'bootstrap read: {err}')
        time.sleep(0.5)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=probe, daemon=True).start()
    webview.start(debug=False, private_mode=False, gui='edgechromium')

    print(f"bridge      : {RESULT['bridge']}")
    print(f"bootstrap   : {str(RESULT['bootstrap'])[:220]}")
    print(f"errors      : {RESULT['errors']}")
    ok = RESULT['bridge'] == 'ok' and RESULT['bootstrap'] and not str(RESULT['bootstrap']).startswith('ERR')
    print('[OK] gui shell works' if ok else '[FAIL] gui shell has problems')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
