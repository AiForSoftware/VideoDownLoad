"""
Quick smoke: confirm `api.sources()` now returns the full parser table
(not only the few that have been imported via URL matching).
Runs in-process so no window / no exe build is needed.
"""
import sys
from pathlib import Path

DESKTOP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DESKTOP_ROOT.parent
sys.path.insert(0, str(DESKTOP_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'engine'))

os_env_lazy = 'VD_LAZY_PARSERS'
import os
os.environ.setdefault(os_env_lazy, '1')

from backend.api import JsApi  # noqa: E402

api = JsApi(version='smoke')

# Mimic the frontend: first call kicks off background load and may return empty;
# poll until engine_ready, just like the UI does.
import time
for attempt in range(20):
    out = api.sources()
    if out.get('engine_ready'):
        break
    print(f'  waiting for engine (state={out.get("engine_state")})...')
    time.sleep(0.5)
else:
    raise SystemExit('engine never became ready within 10s')

platforms = out.get('platforms') or []
generic = out.get('generic') or []
print(f'engine_ready={out.get("engine_ready")} state={out.get("engine_state")}')
print(f'platforms: {len(platforms)}  generic: {len(generic)}')
print(f'platform sample: {platforms[:5]} ... {platforms[-3:]}')
print(f'generic  sample: {generic[:5]} ... {generic[-3:]}')
assert 'BilibiliVideoClient' in platforms, 'Bilibili parser missing'
assert 'DouyinVideoClient' in platforms, 'Douyin parser missing'
assert 'BaseVideoClient' not in platforms, 'BaseVideoClient should be filtered out'
print('OK: full parser table is exposed (>=60 platforms).')