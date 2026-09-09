'''
Function:
    PyInstaller runtime hook
    - put the bundled node runtime on PATH (vd calls `node` for some platforms)
    - make sure a GUI (windowed) process always has usable stdout/stderr streams
Author:
    CodeBuddy
'''
import os
import sys

_MEIPASS = getattr(sys, '_MEIPASS', None)

'''1. node runtime'''
if _MEIPASS:
    node_dir = os.path.join(_MEIPASS, 'nodejs_runtime')
    if os.path.isdir(node_dir):
        current = os.environ.get('PATH', '')
        if node_dir.lower() not in current.lower():
            os.environ['PATH'] = node_dir + os.pathsep + current

'''2. stdio safety'''
if sys.stdout is None or sys.stderr is None:
    try:
        from platformdirs import user_log_dir
        log_dir = os.path.join(user_log_dir(appname='vd-desktop', appauthor='vd'))
    except Exception:
        log_dir = os.path.join(os.path.expanduser('~'), '.vd-desktop')
    try:
        os.makedirs(log_dir, exist_ok=True)
        stream = open(os.path.join(log_dir, 'desktop.log'), 'a', encoding='utf-8', errors='ignore')
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    except Exception:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, 'w', encoding='utf-8')
        if sys.stderr is None:
            sys.stderr = open(os.devnull, 'w', encoding='utf-8')
