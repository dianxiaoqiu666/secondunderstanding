"""Local adaptation of L3's launcher: planning + delivery on isolated ports."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.request import urlopen
import uuid

import psutil

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'
STATE = RUNTIME / 'processes.json'
STOP = RUNTIME / 'stop.request'


def read_owned_supervisor():
    if not STATE.is_file():
        return None
    value = json.loads(STATE.read_text(encoding='utf-8-sig'))
    if Path(value.get('project_root', '')).resolve() != ROOT or value.get('status') == 'stopped':
        return None
    try:
        owner = psutil.Process(value['supervisor'])
        command = owner.cmdline()
        if (abs(owner.create_time() - value['supervisor_created']) > .01
                or Path(owner.cwd()).resolve() != ROOT
                or not any(Path(arg).resolve() == Path(__file__).resolve() for arg in command[1:] if arg.endswith('start.py'))):
            return None
    except (psutil.Error, KeyError, OSError):
        return None
    return value


def request_stop():
    value = read_owned_supervisor()
    if value is None:
        print('No verified running supervisor belongs to this project.', flush=True)
        return
    STOP.write_text(value['run_id'], encoding='utf-8')
    print('Stop requested for this project only.', flush=True)


def write_state(value):
    temporary = RUNTIME / 'processes.pending.json'
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(STATE)


def run(args):
    os.chdir(ROOT)
    if read_owned_supervisor() is not None:
        raise SystemExit('This project already has a running supervisor.')
    if args.ui_port == args.planning_port:
        raise SystemExit('UI and planning ports must differ.')
    upstream = urlparse(args.understanding_url)
    if (upstream.scheme != 'http' or upstream.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or upstream.username or upstream.password):
        raise SystemExit('Legacy S1 URL must be a local HTTP address without credentials.')
    services = [('planning', args.planning_port), ('delivery', args.ui_port)]
    for _, port in services:
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError as error:
                raise SystemExit(f'Port {port} is occupied; select another port. No existing service was stopped.') from error
    RUNTIME.mkdir(exist_ok=True)
    (ROOT / '.tmp').mkdir(exist_ok=True)
    env = dict(os.environ, TEMP=str(ROOT / '.tmp'), TMP=str(ROOT / '.tmp'),
               PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
               L3_PLANNING_URL=f'http://127.0.0.1:{args.planning_port}',
               L3_UNDERSTANDING_URL=args.understanding_url)
    # Never inherit another project's module search path.
    env.pop('PYTHONPATH', None)
    children, logs = [], []
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    value = {'project_root': str(ROOT), 'run_id': uuid.uuid4().hex,
             'supervisor': os.getpid(), 'supervisor_created': psutil.Process().create_time(),
             'status': 'starting', 'services': {}, 'ports': dict(services), 'service_identity': {}}
    try:
        for name, port in services:
            log = (RUNTIME / f'{name}.log').open('a', encoding='utf-8')
            logs.append(log)
            child = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', f'services.{name}.app:app',
                                      '--host', '127.0.0.1', '--port', str(port)],
                                     cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            children.append(child)
            value['services'][name] = child.pid
            value['service_identity'][name] = {'pid': child.pid, 'created': psutil.Process(child.pid).create_time()}
        write_state(value)
        deadline = time.monotonic() + 40
        ready = set()
        while not stopping and len(ready) < len(services) and time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('A service exited; inspect this project runtime logs.')
            for name, port in services:
                try:
                    with urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                        result = json.load(response)
                        if result.get('status') == 'ok' and result.get('service') == name:
                            ready.add(name)
                except (OSError, ValueError):
                    pass
            time.sleep(.2)
        if stopping:
            return
        if len(ready) != len(services):
            raise RuntimeError('Service startup timed out; inspect this project runtime logs.')
        value['status'] = 'ready'
        write_state(value)
        print(f'SHELF DESIGN READY: http://127.0.0.1:{args.ui_port}/algorithm', flush=True)
        print('CAD upload uses the separate legacy S1 contract. No S1 service is started here.', flush=True)
        while not stopping:
            if STOP.is_file() and STOP.read_text(encoding='utf-8-sig').strip() == value['run_id']:
                break
            if any(child.poll() is not None for child in children):
                raise RuntimeError('A service exited; stopping owned children.')
            time.sleep(.5)
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    owner = psutil.Process(child.pid)
                    owned = owner.children(recursive=True) + [owner]
                    for process in reversed(owned):
                        try:
                            process.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    _, survivors = psutil.wait_procs(owned, timeout=3)
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
        value['status'] = 'stopped'
        write_state(value)
        if STOP.is_file() and STOP.read_text(encoding='utf-8-sig').strip() == value['run_id']:
            STOP.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--ui-port', type=int, default=8130, choices=range(1024, 65536), metavar='PORT')
    parser.add_argument('--planning-port', type=int, default=8132, choices=range(1024, 65536), metavar='PORT')
    parser.add_argument('--understanding-url', default='http://127.0.0.1:8131')
    arguments = parser.parse_args()
    request_stop() if arguments.stop else run(arguments)
