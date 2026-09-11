"""Read observed uploaded CAD designs. No planning, business I/O or reference I/O.

Only evidence-backed shelf modules enter the observed BOM. Display dimensions
are separately labelled assumptions and never become observed material data.
"""
from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter

import ezdxf
from ezdxf import disassemble
from shapely import STRtree, set_precision
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

# Display reconstruction precision, not a production collision allowance.
GRID_MM = 0.1
SUPPORT_MM = 0.11  # exceeds maximum 0.1-mm-grid endpoint displacement
SPEC_TOL_MM = 1.0
SHELF_WORDS = ('货架', 'shelf', 'shelves', 'racking')


def _shelf_name(value):
    return any(word in value.lower() for word in SHELF_WORDS)


def _point(value):
    p = tuple(float(v) for v in value)
    if not all(math.isfinite(v) for v in p) or (len(p) > 2 and abs(p[2]) > 1e-8):
        raise ValueError('NON_PLANAR_OR_NONFINITE_COORDINATE')
    return list(p[:2])


def _plane(entity):
    attrs = entity.dxf.all_existing_dxf_attribs()
    if tuple(attrs.get('extrusion', (0, 0, 1))) != (0, 0, 1):
        raise ValueError('NON_PLANAR_EXTRUSION')
    elevation = attrs.get('elevation', 0)
    if isinstance(elevation, (int, float)):
        if not math.isfinite(elevation) or abs(elevation) > 1e-8:
            raise ValueError('NONZERO_ELEVATION')
    else:
        _point(elevation)


def _spec(text):
    match = re.fullmatch(r'\s*(?:线下|货架|shelf\s*)?\s*(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*(mm|m)\s*(?:货架)?\s*', text, re.I)
    if not match:
        return None
    scale = 1000 if match[3].lower() == 'm' else 1
    values = [float(match[1]) * scale, float(match[2]) * scale]
    return sorted(values, reverse=True) if min(values) > 0 else None


def _polyline(e):
    _plane(e)
    if e.dxftype() == 'LWPOLYLINE':
        vs = list(e.get_points())
        if e.dxf.const_width or any(v[2] or v[3] or v[4] for v in vs):
            raise ValueError('CURVED_OR_WIDE_POLYLINE')
        return [_point(v[:2]) for v in vs], bool(e.closed)
    if not e.is_2d_polyline or any(v.dxf.get('bulge', 0) or v.dxf.get('start_width', 0) or v.dxf.get('end_width', 0) for v in e.vertices):
        raise ValueError('UNSUPPORTED_POLYLINE')
    return [_point(v.dxf.location) for v in e.vertices], bool(e.is_closed)


def _parts(entity, doc, ancestors=()):
    if entity.dxftype() != 'INSERT':
        return [entity]
    name = entity.dxf.name
    block = doc.blocks.get(name)
    if name in ancestors or len(ancestors) >= 32 or block is None or block.block.is_xref:
        raise ValueError('MISSING_EXTERNAL_OR_RECURSIVE_BLOCK')
    if entity.has_extension_dict and 'ACAD_FILTER' in entity.get_extension_dict():
        raise ValueError('CLIPPED_BLOCK')
    if entity.mcount > 1:
        raise ValueError('MULTI_INSERT_UNSUPPORTED')
    for child in block:
        if child.dxftype() == 'INSERT':
            _parts(child, doc, (*ancestors, name))
    return list(disassemble.recursive_decompose([entity]))


def _module(poly, identifier, handles, kind):
    if poly.geom_type != 'Polygon' or not poly.is_valid or poly.area <= 0 or poly.interiors:
        raise ValueError('AMBIGUOUS_MODULE_FOOTPRINT')
    rect = poly.minimum_rotated_rectangle
    if poly.area / rect.area < .998:
        raise ValueError('NONRECTANGULAR_MODULE_FOOTPRINT')
    points = list(rect.exterior.coords)
    edges = [(math.dist(a, b), a, b) for a, b in zip(points, points[1:])]
    length, a, b = max(edges)
    depth = min(edge[0] for edge in edges)
    angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180
    if min(angle, 180-angle) < .001:
        angle = 0.0
    return dict(id=identifier, classification='UNKNOWN', x_mm=poly.centroid.x,
                y_mm=poly.centroid.y, length_mm=length, depth_mm=depth,
                rotation_deg=angle, footprint_mm=[list(p) for p in poly.exterior.coords],
                source_handles=sorted(set(handles)), material_id=None,
                default_level_count=None, measured_height_mm=None, type=kind)


def _block_module(entity, doc):
    edges = []
    for e in _parts(entity, doc):
        if e.dxftype() == 'LINE':
            edges.append(LineString([_point(e.dxf.start), _point(e.dxf.end)]))
        elif e.dxftype() in {'LWPOLYLINE', 'POLYLINE'}:
            pts, closed = _polyline(e)
            edges.append(LineString(pts + ([pts[0]] if closed else [])))
        elif e.dxftype() not in {'TEXT', 'MTEXT', 'ATTDEF', 'ATTRIB'}:
            raise ValueError('UNSUPPORTED_SHELF_BLOCK_CONTENT')
    faces = list(polygonize(unary_union(edges)))
    if not faces:
        raise ValueError('NO_CLOSED_MODULE_FOOTPRINT')
    # A block is one module only when its full face union is one rectangle.
    result = _module(unary_union(faces), 'insert-' + entity.dxf.handle, [entity.dxf.handle], 'INSERT')
    result['block_name'] = entity.dxf.name
    return result


def _line_modules(records):
    if not records:
        return [], []
    lines = [LineString([r['start_mm'], r['end_mm']]) for r in records]
    tree = STRtree(lines)
    faces = sorted(polygonize(unary_union([set_precision(line, GRID_MM) for line in lines])), key=lambda p: (p.centroid.x, p.centroid.y))
    modules, unknown = [], []
    for i, poly in enumerate(faces):
        nearby = tree.query(poly.envelope.buffer(SUPPORT_MM * 2))
        indices = [int(j) for j in nearby if lines[int(j)].buffer(SUPPORT_MM).intersection(poly.boundary).length > .000001]
        handles = [records[j]['handle'] for j in indices]
        support = unary_union([lines[j].buffer(SUPPORT_MM) for j in indices])
        try:
            if poly.boundary.difference(support).length > .001:
                raise ValueError('UNSUPPORTED_MODULE_PERIMETER')
            module = _module(poly, f'line-face-{i+1}', handles, 'LINE_FACE')
            module['perimeter_source_support_mm'] = SUPPORT_MM
            modules.append(module)
        except ValueError as exc:
            unknown.append(dict(id=f'line-face-{i+1}', type='LINE_FACE', source_handles=handles,
                                footprint_mm=[list(p) for p in poly.exterior.coords], reason=str(exc)))
    return modules, unknown


def _classify(modules, texts):
    labels = [{**t, 'spec': _spec(t['text'])} for t in texts if _spec(t['text'])]
    polygons = {m['id']: Polygon(m['footprint_mm']) for m in modules}
    matches = {}
    for t in labels:
        candidates = []
        for m in modules:
            p = polygons[m['id']]; x0, y0, x1, y1 = p.bounds
            tx, ty = t['position_mm']
            if (abs(m['length_mm'] - t['spec'][0]) <= SPEC_TOL_MM and
                abs(m['depth_mm'] - t['spec'][1]) <= SPEC_TOL_MM and
                abs(m['rotation_deg']) <= .01 and tx < x0 and x0-tx < 3*m['length_mm'] and
                y0-SPEC_TOL_MM <= ty <= y1+SPEC_TOL_MM):
                candidates.append((p.distance(Point(tx, ty)), m['id']))
        if candidates:
            matches[min(candidates)[1]] = t
    # Multi-specification panel establishes a legend context. A lone explicit
    # '线下/shelf' label can also establish its own detached sample.
    panel_specs = {tuple(t['spec']) for t in matches.values()}
    verified_specs = []
    for m in modules:
        p = polygons[m['id']]
        nearest = min((p.distance(q) for key, q in polygons.items() if key != m['id']), default=math.inf)
        t = matches.get(m['id'])
        m['evidence'] = {'nearest_module_edge_mm': nearest if math.isfinite(nearest) else None}
        if t:
            m['evidence']['dimension_label'] = t
        explicit = t and ('线下' in t['text'] or _shelf_name(t['text']))
        if t and nearest > SPEC_TOL_MM and (len(panel_specs) >= 2 or explicit):
            m.update(classification='LEGEND', reason='Detached dimension-matched sample with a labelled panel or explicit shelf-sample annotation')
            verified_specs.append(t['spec'])
    for m in modules:
        if m['classification'] == 'LEGEND':
            continue
        peers = [n for n in modules if n is not m and abs(n['length_mm']-m['length_mm']) <= SPEC_TOL_MM and abs(n['depth_mm']-m['depth_mm']) <= SPEC_TOL_MM]
        family = [n for n in [m, *peers] if n.get('block_name') == m.get('block_name') and n['classification'] != 'LEGEND']
        family_has_contact = any(polygons[a['id']].distance(polygons[b['id']]) <= SPEC_TOL_MM
                                 for i, a in enumerate(family) for b in family[i+1:])
        matches_spec = next((s for s in verified_specs if abs(m['length_mm']-s[0]) <= SPEC_TOL_MM and abs(m['depth_mm']-s[1]) <= SPEC_TOL_MM), None)
        if m['id'] in matches:
            m['reason'] = 'Dimension-labelled object without sufficient detached sample evidence'
        elif m['type'] == 'INSERT' and peers and family_has_contact:
            m.update(classification='ACTUAL', reason='Repeated explicitly named shelf block with connected family instances outside identified dimension sample panel')
        elif m['type'] == 'LINE_FACE' and matches_spec and peers and m['evidence']['nearest_module_edge_mm'] is not None and m['evidence']['nearest_module_edge_mm'] <= SPEC_TOL_MM:
            m.update(classification='ACTUAL', reason='Closed source-supported module matching an evidenced shelf sample and touching another module; no synthetic divider')
        else:
            m['reason'] = 'Shelf versus sample/fixture or individual module identity cannot be established'
        if matches_spec:
            m['nominal_specification_mm'] = matches_spec
    return modules


def extract_existing_design(data: bytes, filename: str) -> dict:
    """Pure uploaded-byte extraction; classification authority belongs to caller."""
    if not data or len(data) > 25*1024*1024:
        raise ValueError('CAD_SIZE_INVALID: DXF must be nonempty and at most 25 MB')
    try:
        doc = ezdxf.read(io.StringIO(data.decode('utf-8-sig'), newline=None))
    except Exception as exc:
        raise ValueError('CAD_PARSE_FAILED: UTF-8 DXF is required') from exc
    if doc.units != 4:
        raise ValueError('UNITS_NOT_MM: INSUNITS must explicitly be 4')
    from services.understanding.app import hatch_polygons, polygon_data
    drawing = dict(lines=[], polylines=[], texts=[], bounds_mm=[])
    space = dict(boundary=None, barriers=[], exclusions=[], entrances=[])
    unknown, annotations, candidates, shelf_lines, scopes, holes = [], [], [], [], [], []
    accounted = {}
    def unsupported(e, reason):
        unknown.append(dict(type=e.dxftype(), handle=e.dxf.handle, source_handles=[e.dxf.handle], reason=reason))
        accounted[e.dxf.handle] = 'UNKNOWN'
    for original in doc.modelspace():
        handle, kind, layer = original.dxf.handle, original.dxftype(), original.dxf.layer
        try:
            _plane(original)
            if kind == 'INSERT' and _shelf_name(original.dxf.name):
                candidates.append(_block_module(original, doc))
                accounted[handle] = 'SHELF_CANDIDATE'
            if kind == 'DIMENSION':
                annotations.append(dict(handle=handle, type=kind, layer=layer))
                accounted[handle] = 'ANNOTATION'
                continue
            parts = _parts(original, doc)
            if not parts:
                unsupported(original, 'EMPTY_BLOCK_WITHOUT_GEOMETRY')
            for e in parts:
                typ = e.dxftype(); effective_layer = layer if e.dxf.layer == '0' else e.dxf.layer
                role = 'WALL' if effective_layer.upper() in {'墙体', 'WALL', 'WALLS'} else 'UNKNOWN'
                if typ == 'LINE':
                    item = dict(start_mm=_point(e.dxf.start), end_mm=_point(e.dxf.end), handle=handle, layer=effective_layer, role=role)
                    drawing['lines'].append(item)
                    if role == 'WALL':
                        space['barriers'].append(dict(id=f'wall-{handle}-{len(space["barriers"])}', start_mm=item['start_mm'], end_mm=item['end_mm']))
                        accounted[handle] = 'WALL'
                    elif kind == 'LINE' and _shelf_name(layer):
                        shelf_lines.append(item); accounted[handle] = 'SHELF_LINE_EVIDENCE'
                    elif handle not in accounted:
                        unsupported(original, 'LINE_SEMANTICS_UNKNOWN')
                elif typ in {'TEXT', 'MTEXT', 'ATTRIB', 'ATTDEF'}:
                    text = e.plain_text() if hasattr(e, 'plain_text') else e.dxf.text
                    drawing['texts'].append(dict(position_mm=_point(e.dxf.insert), text=text, handle=handle, layer=effective_layer))
                    if kind != 'INSERT':
                        annotations.append(dict(handle=handle, type=typ, layer=effective_layer)); accounted[handle] = 'ANNOTATION'
                elif typ in {'LWPOLYLINE', 'POLYLINE'}:
                    pts, closed = _polyline(e)
                    drawing['polylines'].append(dict(points_mm=pts, closed=closed, handle=handle, layer=effective_layer, role=role))
                    p = Polygon(pts) if closed else None
                    if p is not None and p.is_valid and p.area > 0:
                        obj = dict(id=f'poly-{handle}', **polygon_data(p))
                        upper = effective_layer.upper()
                        if upper in {'PLANNING_SCOPE', 'HUMAN_PLANNING_SCOPE'}:
                            scopes.append(p); accounted[handle] = 'EXPLICIT_SCOPE'
                        elif upper in {'PLANNING_HOLE', 'PLANNING_HOLES'}:
                            holes.append(list(p.exterior.coords)); accounted[handle] = 'EXPLICIT_HOLE'
                        elif upper in {'EXCLUSION', 'EXCLUSION_ZONE', 'NO_PLACEMENT'}:
                            space['exclusions'].append(obj); accounted[handle] = 'EXCLUSION'
                        elif upper in {'ENTRANCE', 'ENTRANCES', '入口'}:
                            space['entrances'].append(obj); accounted[handle] = 'ENTRANCE'
                        elif handle not in accounted:
                            unsupported(original, 'POLYGON_SEMANTICS_UNKNOWN')
                    elif handle not in accounted:
                        unsupported(original, 'OPEN_OR_INVALID_POLYLINE')
                elif typ == 'HATCH':
                    for i, p in enumerate(hatch_polygons(e)):
                        obj = dict(id=f'hatch-{handle}-{i}', **polygon_data(p))
                        space['exclusions'].append(obj)
                        for ring in [obj['boundary_mm'], *obj['holes_mm']]:
                            drawing['polylines'].append(dict(points_mm=ring, closed=True, handle=handle, layer=effective_layer, role='HATCH'))
                    accounted[handle] = 'HATCH_OBSERVED'
                else:
                    unsupported(original, 'UNSUPPORTED_ENTITY_CONTENT:'+typ)
        except Exception as exc:
            unsupported(original, f'{type(exc).__name__}: {exc}')
    if len(scopes) == 1:
        proposed = Polygon(scopes[0].exterior.coords, holes)
        if proposed.is_valid and proposed.area > 0 and all(scopes[0].contains(Polygon(h)) for h in holes):
            space['boundary'] = polygon_data(proposed)
    line_candidates, face_unknown = _line_modules(shelf_lines)
    candidates += line_candidates; unknown += face_unknown
    modules = _classify(candidates, drawing['texts'])
    shelves = [m for m in modules if m['classification'] == 'ACTUAL']
    legends = [m for m in modules if m['classification'] == 'LEGEND']
    unknown += [m for m in modules if m['classification'] == 'UNKNOWN']
    covered_lines = {h for m in modules for h in m['source_handles']}
    for line in shelf_lines:
        if line['handle'] not in covered_lines:
            unknown.append(dict(handle=line['handle'], type='LINE', source_handles=[line['handle']], reason='NO_SUPPORTED_INDIVIDUAL_MODULE_FACE'))
    counts = Counter()
    dimension_bases = {}
    for m in shelves:
        # Textual specification may supply nominal XY; otherwise measured XY.
        dims = m.get('nominal_specification_mm', [round(m['length_mm'], 3), round(m['depth_mm'], 3)])
        counts[tuple(dims)] += 1
        dimension_bases.setdefault(tuple(dims), set()).add('NOMINAL_SOURCE_LABEL' if 'nominal_specification_mm' in m else 'MEASURED_ROUNDED_0.001MM')
    bom = [dict(id=f'observed-{i+1}', material_id=None, length_mm=k[0], depth_mm=k[1], default_level_count=None, quantity=v,
                source='OBSERVED_CAD', dimension_basis=next(iter(dimension_bases[k])) if len(dimension_bases[k]) == 1 else 'MIXED_NOMINAL_SOURCE_LABEL_AND_MEASURED_ROUNDED_0.001MM')
           for i, (k, v) in enumerate(sorted(counts.items()))]
    points = [p for l in drawing['lines'] for p in [l['start_mm'], l['end_mm']]] + [p for l in drawing['polylines'] for p in l['points_mm']] + [t['position_mm'] for t in drawing['texts']]
    if points:
        drawing['bounds_mm'] = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]
    # One unknown record can explain several faces but never duplicate identical entity errors.
    unique = {repr(sorted(u.items())): u for u in unknown}
    unknown = list(unique.values())
    reason_text = {
        'LINE_SEMANTICS_UNKNOWN': '原线保留显示，其空间用途无法可靠分类。',
        'NO_SUPPORTED_INDIVIDUAL_MODULE_FACE': '原线未形成有完整边线证据的单个货架模块，不计物料。',
        'NONRECTANGULAR_MODULE_FOOTPRINT': '轮廓不是可靠的单个矩形货架，保留为未知对象。',
        'Shelf versus sample/fixture or individual module identity cannot be established': '无法可靠区分货架、图例或其他设施，不计物料。',
        'Dimension-labelled object without sufficient detached sample evidence': '存在尺寸文字，但不足以确定实际货架或图例身份。',
        'Detached dimension-matched sample with a labelled panel or explicit shelf-sample annotation': '孤立对象匹配规格文字及图例区域证据，作为图例保留。',
        'Repeated explicitly named shelf block with connected family instances outside identified dimension sample panel': '具有明确货架块名和成组连接实例，且不属于已识别图例。',
        'Closed source-supported module matching an evidenced shelf sample and touching another module; no synthetic divider': '原线完整围合单模块并匹配规格图例，存在相邻模块；未新增分隔线。',
    }
    for item in [*shelves, *legends, *unknown]:
        if 'reason' in item:
            original_reason = item['reason']
            item['reason_code'] = original_reason
            item['reason'] = reason_text.get(original_reason, '对象暂不支持可靠转换，已保留源句柄；详情：' + original_reason)
    omissions = ['货架真实高度与默认层数未提供；三维显示参数是单独标明的展示假设。']
    if space['boundary'] is None:
        omissions.append('NO_RELIABLE_PLANNING_SCOPE：没有可靠范围，仅显示已有对象，不补造地面或外壳。')
    if not shelves:
        omissions.append('NO_IDENTIFIABLE_SHELVES：未识别出实际货架；空块定义不是货架实例。')
    if unknown:
        omissions.append('PARTIAL_EXTRACTION：仅提取证据可靠部分，未知对象不计入已有设计物料清单。')
    return dict(schema_version='existing-1.0', task_purpose='existing_design',
                source=dict(filename=filename, sha256=hashlib.sha256(data).hexdigest()),
                source_classification='UNCLASSIFIED_UPLOAD', units='mm', drawing=drawing, space=space,
                shelves=shelves, legend_samples=legends, unknown_objects=unknown, bom=bom,
                summary=dict(shelf_count=len(shelves), legend_count=len(legends), unknown_count=len(unknown),
                             annotation_count=len(annotations), wall_count=len(space['barriers']), exclusion_count=len(space['exclusions']),
                             source_entity_count=len(doc.modelspace()), omissions=omissions),
                evidence=dict(annotations=annotations, explicit_holes=holes, source_entity_roles=accounted,
                              parser='ezdxf', geometry_kernel='Shapely/GEOS', raw_drawing_coordinates_preserved=True,
                              footprint_precision_note='INSERT 轮廓采用 ezdxf 世界坐标；LINE 面采用 0.1 mm GEOS 精度模型，全部周长核对原线，原始端点保留在 drawing。',
                              precision_grid_mm=GRID_MM, unsupported_geometry_not_filled=True),
                display_parameters=dict(wall_height_mm=2600, wall_thickness_mm=100, shelf_height_mm=1800,
                                        shelf_level_count=4, meaning='DISPLAY_ONLY'))
