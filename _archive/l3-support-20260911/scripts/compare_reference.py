"""Post-generation audit only. Never imported by runtime services."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

import ezdxf

ROOT = Path(__file__).resolve().parents[1]

def compare(layout_path):
    # Freeze the generated answer before opening any historical design.
    frozen_bytes = layout_path.read_bytes()
    result = json.loads(frozen_bytes)
    assert result['validation']['status'] == 'PASS'
    frozen_hash = hashlib.sha256(frozen_bytes).hexdigest()
    reference = next((ROOT / 'input/reference/cad').glob('*.dxf'))
    doc = ezdxf.readfile(reference)
    entities = list(doc.modelspace())
    types = dict(Counter(e.dxftype() for e in entities))
    inserts = Counter(e.dxf.layer for e in entities if e.dxftype() == 'INSERT')
    def line_key(start, end):
        return tuple(sorted((tuple(round(v, 6) for v in start[:2]), tuple(round(v, 6) for v in end[:2]))))
    generated_walls = {line_key(w['start_mm'], w['end_mm']) for w in result['spatial']['walls']}
    reference_walls = {line_key(tuple(e.dxf.start), tuple(e.dxf.end)) for e in entities
                       if e.dxftype() == 'LINE' and e.dxf.layer == '墙体'}
    # Read-only inventory of every warehouse file. No coordinates are transferred.
    inventory = []
    for p in sorted((ROOT / '数据仓库').rglob('*')):
        if not p.is_file():
            continue
        item = {'path': str(p.relative_to(ROOT)), 'size': p.stat().st_size,
                'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
        suffix = p.suffix.lower()
        if suffix in {'.dxf', '.dxf~'}:
            drawing = ezdxf.readfile(p)
            item['modelspace_entity_count'] = len(drawing.modelspace())
        elif suffix == '.json':
            value = json.loads(p.read_text(encoding='utf-8-sig'))
            item['json_type'] = type(value).__name__
            item['top_level_keys'] = list(value) if isinstance(value, dict) else []
        elif suffix in {'.xlsx', '.docx'}:
            with zipfile.ZipFile(p) as archive:
                item['archive_member_count'] = len(archive.namelist())
                item['archive_crc_error'] = archive.testzip()
        elif suffix == '.sqlite':
            with sqlite3.connect(p.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
                connection.execute('PRAGMA query_only=ON')
                item['sqlite_integrity'] = connection.execute('PRAGMA integrity_check').fetchone()[0]
                item['tables'] = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        elif suffix == '.md':
            item['text_characters'] = len(p.read_text(encoding='utf-8-sig'))
        else:
            item['inspection'] = 'binary signature, byte size and SHA256 inventory; not a production data source'
            item['signature_hex'] = p.read_bytes()[:16].hex()
        inventory.append(item)
    assert layout_path.read_bytes() == frozen_bytes
    return {'mode': 'POST_GENERATION_READ_ONLY', 'frozen_layout_file': str(layout_path.relative_to(ROOT)),
            'frozen_layout_sha256': frozen_hash, 'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
            'reference_entity_types': types, 'reference_insert_counts_by_layer': dict(inserts),
            'generated_shelves': len(result['shelves']), 'shared_unique_wall_lines': len(generated_walls & reference_walls),
            'production_only_unique_wall_lines': len(generated_walls - reference_walls),
            'reference_only_unique_wall_lines': len(reference_walls - generated_walls),
            'conclusion': 'Reference inserts include drawing legends and are not a target shelf count. No reference coordinates enter generation; comparison does not prove commercial layout quality.',
            'warehouse_inventory': inventory, 'generated_result_unchanged': True}

if __name__ == '__main__':
    candidate = (ROOT / (sys.argv[1] if len(sys.argv) > 1 else 'outputs/first_layout.json')).resolve()
    if not candidate.is_relative_to(ROOT):
        raise SystemExit('Only project-local layout files are accepted')
    report = compare(candidate)
    (ROOT / 'outputs/reference_comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'warehouse_inventory'}, ensure_ascii=False, indent=2))
