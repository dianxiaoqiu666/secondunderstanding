"""Rebuild a local venv using the locked packages already installed in L3."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import venv

from supplement_from_l3 import safe_path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'docs/migration/2026-09-11-runtime-manifest.json'


def name_key(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(source):
    destination = ROOT / '.venv'
    if destination.exists() or REPORT.exists():
        raise SystemExit('Local environment already exists; refusing to replace it.')
    source = source.resolve()
    source_site = safe_path(source, '.venv/Lib/site-packages')
    source_python = safe_path(source, '.venv/Scripts/python.exe')
    probe = 'import json,struct,sys,sysconfig;print(json.dumps([sys.implementation.cache_tag,sysconfig.get_platform(),struct.calcsize("P")]))'
    source_abi = json.loads(subprocess.check_output([str(source_python), '-I', '-B', '-c', probe], text=True))
    local_abi = [sys.implementation.cache_tag, sysconfig.get_platform(), struct.calcsize('P')]
    if source_abi != local_abi:
        raise SystemExit(f'Python ABI mismatch: {source_abi} / {local_abi}')
    installed = {name_key(dist.metadata['Name']): dist for dist in metadata.distributions(path=[str(source_site)])}
    packages, files = [], {}
    for line in (ROOT / 'requirements.lock.txt').read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        name, version = line.strip().split('==')
        dist = installed.get(name_key(name))
        if dist is None or dist.version != version:
            raise SystemExit(f'Locked package not present in source: {line}')
        packages.append({'name': name, 'version': version})
        for relative in dist.files or []:
            origin = (source_site / relative).resolve()
            # Venv launchers contain absolute paths. Rebuild the venv and use -m.
            if not origin.is_relative_to(source_site) or '__pycache__' in origin.parts or origin.suffix in {'.pyc', '.pyo'}:
                continue
            normalized = origin.relative_to(source_site).as_posix()
            safe_path(source_site, normalized)
            if not origin.is_file():
                raise SystemExit(f'Installed package file is missing: {normalized}')
            files[normalized] = {'path': normalized, 'bytes': origin.stat().st_size,
                                 'sha256': sha(origin), 'source_mtime_ns': origin.stat().st_mtime_ns}
    # Every source dependency has been checked before creating the target venv.
    venv.EnvBuilder(with_pip=True).create(destination)
    target_site = destination / 'Lib/site-packages'
    for relative, record in files.items():
        target = safe_path(target_site, relative)
        if target.exists():
            raise SystemExit(f'Unexpected file in new environment: {relative}')
        target.parent.mkdir(parents=True, exist_ok=True)
        with (source_site / relative).open('rb') as incoming, target.open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    mismatches = []
    for relative, record in files.items():
        original, target = source_site / relative, target_site / relative
        if sha(original) != record['sha256'] or original.stat().st_mtime_ns != record['source_mtime_ns']:
            mismatches.append({'path': relative, 'reason': 'SOURCE_CHANGED'})
        if sha(target) != record['sha256']:
            mismatches.append({'path': relative, 'reason': 'TARGET_MISMATCH'})
    value = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'source_site_packages': str(source_site),
             'destination_site_packages': str(target_site), 'python_abi': local_abi,
             'packages': packages, 'file_count': len(files), 'copied_bytes': sum(row['bytes'] for row in files.values()),
             'files': list(files.values()), 'status': 'PASS' if not mismatches else 'FAIL', 'errors': mismatches,
             'method': 'New local venv; locked distribution files copied and SHA256 verified. No source venv launchers, cache, credentials or browser binaries copied.'}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value[key] for key in ('status', 'file_count', 'copied_bytes', 'errors')}, ensure_ascii=False))
    if mismatches:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    main(parser.parse_args().source)
