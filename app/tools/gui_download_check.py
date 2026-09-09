'''
GUI download integration check.
Opens a real pywebview window, drives parse + download through the JS bridge
exactly like the real GUI does, and records the round-trip time for each call.
If `download` blocks the bridge thread on a heavy synchronous rebuild, this
will hang and we will see the elapsed time balloon.
'''
from __future__ import annotations
import sys, time, threading, json
from pathlib import Path

DESKTOP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DESKTOP_ROOT))

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import webview  # noqa: E402
from backend.api import JsApi  # noqa: E402

TEST_URL = 'https://www.bilibili.com/video/BV1UhM46LEBL'
RESULT: dict = {'bridge': None, 'parse': None, 'download': None, 'errors': []}


def _wait_for_js(window, expr: str, poll_expr: str, timeout: float, tag: str) -> dict:
    t0 = time.time()
    window.evaluate_js(expr)
    deadline = t0 + timeout
    while time.time() < deadline:
        try:
            r = window.evaluate_js(poll_expr)
        except Exception as err:
            return {'result': f'EVAL_ERR: {err}', 'elapsed': round(time.time() - t0, 1)}
        if r is not None and r != 'pending' and r != '':
            return {'result': str(r)[:400], 'elapsed': round(time.time() - t0, 1)}
        time.sleep(0.5)
    return {'result': f'TIMEOUT_{tag}', 'elapsed': round(time.time() - t0, 1)}


def main() -> int:
    api = JsApi(version='gui-dl-check')
    index = DESKTOP_ROOT / 'web' / 'index.html'
    window = webview.create_window('VideoDL DL Check', index.as_uri(), js_api=api,
                                  width=1240, height=820)
    api.bindwindow(window)

    def probe():
        # give the page time to load the bridge and finish bootstrap()
        time.sleep(5)
        try:
            RESULT['bridge'] = window.evaluate_js(
                "(window.pywebview && window.pywebview.api && window.pywebview.api.parse) ? 'ok' : 'missing'"
            )
        except Exception as err:
            RESULT['errors'].append(f'bridge probe: {err}')

        # 1) parse via the bridge. parse() is now async: it kicks off a
        # background job and the result is delivered through
        # window.applyParseResult() (=> window.__lastParse). So we fire it and
        # poll for __lastParse instead of awaiting the (now immediate) return.
        parse_js = (
            "window.__pr = 'pending';"
            "window.__lastParse = null;"
            "window.pywebview.api.parse(" + json.dumps(TEST_URL) + ");"
            "(function wait(){"
            "  var r = window.__lastParse;"
            "  if (r) { window.__pr = JSON.stringify({ok:r.ok, count:(r.items||[]).length, first_key:((r.items||[])[0]||{}).key||''}); return; }"
            "  setTimeout(wait, 400);"
            "})();"
        )
        RESULT['parse'] = _wait_for_js(window, parse_js, 'window.__pr', 90.0, 'parse')

        # 2) download the first parsed item via the bridge
        pr = (RESULT.get('parse') or {}).get('result') or ''
        key = ''
        try:
            j = json.loads(pr)
            key = j.get('first_key') or ''
        except Exception:
            pass
        if key and 'PARSE_ERR' not in pr and 'TIMEOUT' not in pr:
            dl_js = (
                "window.__dr = 'pending';"
                "window.pywebview.api.download([" + json.dumps(key) + "], null)"
                ".then(function(r){ window.__dr = JSON.stringify(r); })"
                ".catch(function(e){ window.__dr = 'DL_ERR:' + e; });"
            )
            RESULT['download'] = _wait_for_js(window, dl_js, 'window.__dr', 60.0, 'download')
        else:
            RESULT['download'] = {'result': 'SKIPPED (parse failed or no key)', 'elapsed': 0.0}

        time.sleep(1.0)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=probe, daemon=True).start()
    try:
        webview.start(debug=False, private_mode=False, gui='edgechromium')
    except Exception as err:
        RESULT['errors'].append(f'webview.start: {err}')

    print('bridge   :', RESULT['bridge'])
    print('parse    :', RESULT['parse'])
    print('download :', RESULT['download'])
    print('errors   :', RESULT['errors'])
    # The verdict: download should resolve within a few seconds. If it
    # hits the timeout (>30s after parse finished), the bridge is blocked.
    dl = RESULT.get('download') or {}
    dlres = dl.get('result') or ''
    ok = (RESULT['bridge'] == 'ok'
          and 'TIMEOUT' not in dlres
          and 'SKIPPED' not in dlres
          and 'ERR' not in dlres
          and dl.get('elapsed', 0) < 10.0)
    print('[OK] download is responsive' if ok else '[FAIL] download is blocked or errored')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())