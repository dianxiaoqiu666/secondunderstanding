"""Add the reviewed L3 shelf-design dependency set without overwriting files."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'docs/migration'
PLAN = REPORT / '2026-09-11-L3-supplement-plan.json'
MANIFEST = REPORT / '2026-09-11-L3-supplement-manifest.json'
VERIFICATION = REPORT / '2026-09-11-L3-supplement-verification.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_path(root, relative):
    path = root / relative
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise ValueError(f'Path leaves authorized root: {relative}')
    for parent in (path, *path.parents):
        if parent.exists() and (parent.is_symlink() or getattr(parent.stat(), 'st_file_attributes', 0)
                                & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError(f'Reparse path is not allowed: {parent}')
        if parent == root:
            break
    return path


def git_state(source):
    command = ['git', '-c', f'safe.directory={source.as_posix()}', '-C', str(source)]
    def run(*args):
        return subprocess.check_output([*command, *args], encoding='utf-8', errors='strict').strip()
    return {'head': run('rev-parse', 'HEAD'), 'branch': run('branch', '--show-current'),
            'status': run('status', '--porcelain=v1', '--untracked-files=all')}


def inventory(source):
    selected = {}
    def add(relative, category, destination=None):
        destination = destination or relative
        safe_path(source, relative)
        safe_path(ROOT, destination)
        selected[destination] = {'source_path': relative, 'path': destination, 'category': category}

    groups = {
        'services/planning': 'planning_source', 'shared': 'shared_contract',
        'services/delivery': 'design_ui_and_exports',
        'services/understanding': 'legacy_s1_compatibility_source',
        'tools/benchmarks': 'benchmark_tools_and_synthetic_scenarios',
        'tools/reference_profile': 'reference_diagnostics_source',
        'tests/planning': 'planning_tests', 'tests/planning_v2': 'planning_tests',
        'tests/planning_m09': 'planning_tests', 'tests/planning_m10': 'planning_tests',
        'tests/planning_m11': 'planning_tests', 'tests/products_v2': 'product_tests',
        'tests/audit_v2': 'independent_tests', 'tests/reference_profile': 'reference_tests',
        'tests/delivery': 'delivery_tests', 'tests/offline_guard': 'test_support',
    }
    allowed = {'.py', '.json', '.js', '.html', '.css', '.txt', '.md', '.ps1'}
    for directory, category in groups.items():
        for path in sorted((source / directory).rglob('*')):
            if any(part in {'__pycache__', 'node_modules', '.git', '.venv'} for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() in allowed:
                add(path.relative_to(source).as_posix(), category)
    for relative in ('services/__init__.py', 'tools/__init__.py', 'requirements.lock.txt',
                     '.gitignore', '.gitattributes', 'tests/test_v2_confirmation.py'):
        add(relative, 'shared_support')
    docs = ['ALGORITHM_M01', 'ALGORITHM_M01b', 'ALGORITHM_M02', 'PLANNING_M09',
            'PLANNING_M10', 'PLANNING_M10_RESEARCH', 'PLANNING_M11', 'PLANNING_M11_DOOR',
            'PLANNING_M11_LAYOUT', 'PLANNING_M11_SCOPE', 'PRODUCT_ALGORITHM',
            'API_V2', 'API_CONTRACT', 'WORKBENCH_UI', 'OPERATIONS', 'REFERENCE_PROFILE',
            'REFERENCE_COMPARISON', 'ACCEPTANCE', 'INDEPENDENT_ALGORITHM_AUDIT',
            'INDEPENDENT_OBSTACLE_AUDIT', 'INDEPENDENT_PRODUCT_AUDIT', 'DELIVERY_STATUS']
    for name in docs:
        add(f'docs/{name}.md', 'upstream_design_documentation')
    for relative in ('README.md', 'start.ps1', 'stop.ps1', 'scripts/start.py',
                     'scripts/setup.ps1', 'scripts/export_schemas.py', 'scripts/compare_reference.py',
                     'tests/conftest.py', 'pytest.ini'):
        add(relative, 'original_integration_support', f'_archive/l3-support-20260911/{relative}')
    add('input/production/materials/material_templates.v1.json', 'planning_template_catalog')
    add('DATA_CLASSIFICATION.md', 'source_classification_metadata')
    for directory in ('outputs/iterations/M01', 'outputs/baselines/SYNTHETIC_BASELINE',
                      'outputs/planning_m09/before'):
        for path in sorted((source / directory).glob('*.json')):
            add(path.relative_to(source).as_posix(), 'historical_synthetic_regression_fixture')
    for filename in ('reference_profile.json', 'reference_profile_v1.1.json',
                     'FROZEN_SHA256.txt', 'FROZEN_V1.1_SHA256.txt', 'reference.svg',
                     'reference-v1.1.svg', 'line_evidence.json', 'LINE_EVIDENCE_SHA256.txt'):
        add(f'outputs/reference_profile/{filename}', 'frozen_reference_diagnostics_only')
    return list(sorted(selected.values(), key=lambda entry: entry['path']))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def plan(source):
    if PLAN.exists() or MANIFEST.exists():
        raise ValueError('An import plan already exists; inspect it instead of replacing it.')
    if source.resolve() == ROOT.resolve():
        raise ValueError('Source and destination must differ.')
    state = git_state(source)
    files = inventory(source)
    conflicts = []
    for row in files:
        path = safe_path(source, row['source_path'])
        row.update(bytes=path.stat().st_size, source_sha256=digest(path), source_mtime_ns=path.stat().st_mtime_ns)
        target = safe_path(ROOT, row['path'])
        row['action'] = 'ADD' if not target.exists() else 'KEEP_IDENTICAL' if target.is_file() and digest(target) == row['source_sha256'] else 'CONFLICT'
        if row['action'] == 'CONFLICT':
            conflicts.append(row['path'])
    if git_state(source) != state:
        raise ValueError('Source Git state changed while planning.')
    value = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'source': str(source.resolve()),
             'destination': str(ROOT), 'source_git': state, 'files': files, 'conflicts': conflicts,
             'categories': dict(Counter(row['category'] for row in files)),
             'actions': dict(Counter(row['action'] for row in files)),
             'scope': 'Shelf design UI/export, tests, templates, frozen diagnostics and legacy S1 import dependencies.',
             'excluded': ['raw CAD', 'real product workbook', 'runtime and Human state', 'secrets and tokens',
                          'source Git history', 'node_modules', 'unrelated delivery/CAD acceptance outputs']}
    write_json(PLAN, value)
    print(json.dumps({key: value[key] for key in ('actions', 'categories', 'conflicts')}, ensure_ascii=False))


def copy():
    if MANIFEST.exists():
        raise ValueError('Migration already executed. Use verify.')
    value = json.loads(PLAN.read_text(encoding='utf-8-sig'))
    source = Path(value['source'])
    if value['conflicts'] or git_state(source) != value['source_git']:
        raise ValueError('Conflict or changed source Git state; no files copied.')
    # Check the entire package before writing its first target file.
    for row in value['files']:
        original = safe_path(source, row['source_path'])
        target = safe_path(ROOT, row['path'])
        if digest(original) != row['source_sha256'] or original.stat().st_mtime_ns != row['source_mtime_ns']:
            raise ValueError(f'Source changed; package blocked: {row["source_path"]}')
        if row['action'] == 'ADD' and target.exists():
            raise ValueError(f'Destination appeared; package blocked: {row["path"]}')
        if row['action'] == 'KEEP_IDENTICAL' and (not target.is_file() or digest(target) != row['source_sha256']):
            raise ValueError(f'Existing destination changed: {row["path"]}')
    for row in value['files']:
        if row['action'] == 'KEEP_IDENTICAL':
            continue
        original = safe_path(source, row['source_path'])
        target = safe_path(ROOT, row['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        with original.open('rb') as incoming, target.open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        shutil.copystat(original, target)
    value['copied_at_utc'] = datetime.now(timezone.utc).isoformat()
    write_json(MANIFEST, value)
    verify()


def verify():
    value = json.loads(MANIFEST.read_text(encoding='utf-8-sig'))
    source = Path(value['source'])
    errors = []
    for row in value['files']:
        original = safe_path(source, row['source_path'])
        target = safe_path(ROOT, row['path'])
        if digest(original) != row['source_sha256'] or original.stat().st_mtime_ns != row['source_mtime_ns']:
            errors.append({'path': row['source_path'], 'reason': 'SOURCE_CHANGED'})
        if not target.is_file() or digest(target) != row['source_sha256']:
            errors.append({'path': row['path'], 'reason': 'TARGET_MISMATCH'})
    git_unchanged = git_state(source) == value['source_git']
    result = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'PASS' if not errors and git_unchanged else 'FAIL',
              'file_count': len(value['files']), 'added_count': value['actions'].get('ADD', 0),
              'kept_identical_count': value['actions'].get('KEEP_IDENTICAL', 0),
              'source_git_unchanged': git_unchanged, 'source_sha256_and_mtime_unchanged': not any(e['reason']=='SOURCE_CHANGED' for e in errors),
              'errors': errors}
    write_json(VERIFICATION, result)
    print(json.dumps(result, ensure_ascii=False))
    if result['status'] != 'PASS':
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'copy', 'verify'])
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    if args.action == 'plan':
        if args.source is None:
            parser.error('--source is required when planning')
        plan(args.source)
    elif args.action == 'copy':
        copy()
    else:
        verify()
