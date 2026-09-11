"""Acceptance-only network isolation, loaded in child Python processes via PYTHONPATH."""
import ipaddress
import os
from pathlib import Path
import socket
import sys
import json

if os.environ.get('L3_TEST_OFFLINE') == '1':
    root = Path(__file__).resolve().parents[2]
    report = (root / os.environ.get('L3_ACCEPTANCE_DIR','outputs/acceptance')).resolve()
    if not report.is_relative_to(root):
        raise RuntimeError('Acceptance artifacts must remain inside the project')
    report.mkdir(parents=True, exist_ok=True)
    (report / f'offline-guard-{os.getpid()}.txt').write_text('non-loopback connections denied', encoding='utf-8')
    for service in ('understanding','planning','delivery'):
        if f'services.{service}.app:app' in sys.argv:
            (report/f'offline-guard-{service}.json').write_text(json.dumps({
                'pid':os.getpid(),'run_id':os.environ.get('L3_OFFLINE_RUN_ID')}),encoding='utf-8')
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    def allowed(host):
        if host in (None, 'localhost'):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    def check(host):
        if not allowed(host):
            with (report/'server-external-attempts.log').open('a', encoding='utf-8') as log:
                log.write(str(host)+'\n')
            raise OSError('Acceptance offline guard blocks non-loopback network')
    def connect(sock, address):
        if isinstance(address, tuple):
            check(address[0])
        return original_connect(sock,address)
    def connect_ex(sock, address):
        if isinstance(address, tuple):
            check(address[0])
        return original_connect_ex(sock,address)
    def getaddrinfo(host,*args,**kwargs):
        check(host)
        return original_getaddrinfo(host,*args,**kwargs)
    socket.socket.connect=connect
    socket.socket.connect_ex=connect_ex
    socket.getaddrinfo=getaddrinfo
    def protect_reference(event,args):
        if event=='open' and isinstance(args[0],(str,bytes)):
            candidate=Path(os.fsdecode(args[0])).resolve()
            if candidate.is_relative_to(root/'数据仓库') or candidate.is_relative_to(root/'input/reference'):
                raise PermissionError('Acceptance guard: reference data cannot participate in production')
    sys.addaudithook(protect_reference)
