'''
Function:
    Headless smoke test for the desktop backend (no webview window involved)
    Usage:
        python desktop/tools/smoke_test.py [url]
Author:
    CodeBuddy
'''
from __future__ import annotations

import os
import sys
import time
import json
import shutil
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

DESKTOP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DESKTOP_ROOT))

from backend.core import VideoDlService  # noqa: E402

DEFAULT_URL = 'https://www.bilibili.com/video/BV1GJ411x7h7'


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    work_dir = Path.cwd() / '_smoke_out'
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    service = VideoDlService(version='smoke')
    service.config.work_dir = str(work_dir)
    print(f'[1/5] work dir: {work_dir}')

    print(f'[2/5] parsing url: {url}')
    result = service.parse(url)
    items = result.get('items') or []
    print(f'      parsed {len(items)} item(s), ok={result.get("ok")}, error={result.get("error")}')
    for item in items[:5]:
        print(f'      - {item["title"]} | {item["source"]} | {item["ext"]} | valid={item["valid"]} | {item["save_path"]}')
    if not items:
        print('[FAIL] nothing parsed')
        return 1

    keys = [item['key'] for item in items if item['valid']]
    print(f'[3/5] enqueuing {len(keys)} item(s)')
    job = service.enqueue(keys)
    if not job.get('ok'):
        print(f'[FAIL] enqueue failed: {job}')
        return 1
    job_id = job['job_id']

    print('[4/5] downloading (polling progress)')
    deadline = time.time() + 180
    last_print = 0.0
    current = service._getjob(job_id)
    while current and current['status'] in {'queued', 'downloading', 'cancelling'} and time.time() < deadline:
        state = service.state(0)
        if time.time() - last_print > 0.8:
            last_print = time.time()
            for task in state['progress']:
                percent = task['percent']
                percent_text = f'{percent:.1f}%' if percent is not None else 'unknown'
                print(f'      {task["description"][:40]:<40} {task["kind"]:<14} {percent_text}')
        time.sleep(0.3)

    current = service._getjob(job_id)
    if current is None:
        print('[FAIL] job disappeared')
        return 1
    print(f'      job status: {current["status"]}, done {current["done_count"]}/{current["total_count"]}')

    print('[5/5] verifying files')
    ok = True
    for item in current['items']:
        path = Path(item['save_path'])
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        print(f'      [{item["status"]}] {path.name} exists={exists} size={size}')
        if item['status'] == 'done' and (not exists or size == 0):
            ok = False

    print('\n--- recent logs ---')
    for log in service.logs(0)[-15:]:
        print(f'      {log["time"]} [{log["level"]}] {log["message"][:150]}')

    succeeded = current['status'] == 'done' and current['done_count'] == current['total_count'] and ok
    if succeeded:
        print('\n[OK] smoke test passed')
        return 0
    print(f'\n[FAIL] smoke test failed (status={current["status"]}, files_ok={ok}, done={current["done_count"]}/{current["total_count"]})')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
