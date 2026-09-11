"""Run all three local services, without downloading or installing anything."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
import psutil

ROOT = Path(__file__).resolve().parents[1]
SERVICES = [('understanding', 8101), ('planning', 8102), ('delivery', 8100)]

def main():
    os.chdir(ROOT)
    for _, port in SERVICES:
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError:
                raise SystemExit(f'Port {port} is occupied. Stop the existing service first.')
    runtime = ROOT / 'runtime'
    runtime.mkdir(exist_ok=True)
    stop_request = runtime / 'stop.request'
    stop_request.unlink(missing_ok=True)
    (ROOT / '.tmp').mkdir(exist_ok=True)
    env = dict(os.environ, TEMP=str(ROOT / '.tmp'), TMP=str(ROOT / '.tmp'),
               PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
               PLAYWRIGHT_BROWSERS_PATH=str(ROOT / '.cache/ms-playwright'))
    children, logs = [], []
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for name, port in SERVICES:
            log = (runtime / f'{name}.log').open('a', encoding='utf-8')
            logs.append(log)
            child = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn',
                f'services.{name}.app:app', '--host', '127.0.0.1', '--port', str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            children.append(child)
        (runtime / 'processes.json').write_text(json.dumps({
            'supervisor': os.getpid(),
            'services': {name: child.pid for (name, _), child in zip(SERVICES, children)}
        }, indent=2), encoding='utf-8')
        deadline = time.monotonic() + 40
        ready = set()
        while not stopping and len(ready) < 3 and time.monotonic() < deadline:
            if any(c.poll() is not None for c in children):
                raise RuntimeError('Service exited during startup; inspect runtime/*.log')
            for name, port in SERVICES:
                try:
                    with urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                        if json.load(response).get('status') == 'ok':
                            ready.add(name)
                except (OSError, ValueError):
                    pass
            time.sleep(.2)
        if len(ready) != 3:
            raise RuntimeError('Startup did not complete; inspect runtime/*.log')
        print('L3 READY: http://127.0.0.1:8100  (Ctrl+C stops all three services)', flush=True)
        while not stopping:
            if stop_request.exists():
                break
            if any(c.poll() is not None for c in children):
                raise RuntimeError('A service exited. Stopping the other services.')
            time.sleep(.5)
    finally:
        for child in children:
            if child.poll() is None:
                # venv launchers may have a separate interpreter child on Windows.
                # psutil enumerates only descendants of our live Popen process.
                try:
                    owned = psutil.Process(child.pid)
                    tree = owned.children(recursive=True) + [owned]
                    for process in reversed(tree):
                        try:
                            process.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    _, survivors = psutil.wait_procs(tree, timeout=3)
                    for process in survivors:
                        process.kill()
                except psutil.NoSuchProcess:
                    pass
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in logs:
            log.close()
        stop_request.unlink(missing_ok=True)

if __name__ == '__main__':
    main()
