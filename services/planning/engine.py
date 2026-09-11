"""Conservative deterministic island layouts; no source files or SKU inference."""
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Iterable

from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from shared.contracts import BOMRow, LayoutResult, Shelf, Template, UnderstandingPackage

AISLE_MM = 1200.0
# Strict separation avoids navigation connectivity through a zero-area touching point.
GRID_CLEARANCE_MM = AISLE_MM + 10.0
EPS_MM = 1e-6
MAX_GRID_POINTS = 100000
# GEOS represents round joins using chords. Inflate by the half-segment cosine
# so every chord lies outside the true 600 mm circle, including inset concavities.
NAV_QUAD_SEGS = 16
NAV_RADIUS_MM = (AISLE_MM / 2) / math.cos(math.pi / (4 * NAV_QUAD_SEGS))


class PlanningError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str):
    raise PlanningError(code, message)


def _polygon(data, label: str) -> Polygon:
    rings = [data.boundary_mm, *data.holes_mm]
    for ring in rings:
        if len(ring) < 4 or ring[0] != ring[-1]:
            _fail("INVALID_SPATIAL", f"{label} 必须使用明确闭合的标准轮廓。")
    polygon = Polygon(data.boundary_mm, data.holes_mm)
    if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
        _fail("INVALID_SPATIAL", f"{label} 拓扑无效；布局服务不会修复或推测空间。")
    return polygon


def _parts(geometry) -> list[Polygon]:
    if geometry.geom_type == "Polygon":
        return [geometry] if not geometry.is_empty else []
    return [p for g in getattr(geometry, "geoms", []) for p in _parts(g)]


def _nav_buffer(geometry, direction: int = 1):
    return geometry.buffer(direction * NAV_RADIUS_MM, quad_segs=NAV_QUAD_SEGS)


@dataclass
class Geometry:
    scope: Polygon
    walls: list
    exclusions: list[Polygon]
    navigation_components: list[Polygon]
    navigation: Polygon


def _geometry(package: UnderstandingPackage) -> Geometry:
    scope = _polygon(package.spatial.scope, "Planning Scope")
    walls = []
    for wall in package.spatial.walls:
        line = LineString([wall.start_mm, wall.end_mm])
        if not line.is_valid or line.length <= 0:
            _fail("INVALID_SPATIAL", f"墙 {wall.id} 是无效或零长度线段。")
        walls.append(line)
    exclusions = [_polygon(p, f"禁放区 {p.id}") for p in package.spatial.exclusion_zones]
    obstacles = unary_union([*walls, *exclusions])
    base = _nav_buffer(scope, -1).difference(_nav_buffer(obstacles))
    components = sorted(_parts(base), key=lambda p: (-p.area, p.bounds))
    if not components:
        _fail("NO_SAFE_LAYOUT", "Planning Scope 中没有满足 1200 mm 基础通行宽度的空间。")
    return Geometry(scope, walls, exclusions, components, components[0])


def _templates(package: UnderstandingPackage) -> list[Template]:
    templates = sorted(package.business.templates, key=lambda t: t.material_id)
    if len({t.material_id for t in templates}) != len(templates):
        _fail("INVALID_BUSINESS", "物料模板 material_id 重复，无法唯一核对规格。")
    if package.business.product_count != len(package.business.products):
        _fail("INVALID_BUSINESS", "product_count 与标准商品记录数不一致。")
    return templates


def _footprint(x: float, y: float, template: Template, rotation: int) -> Polygon:
    width, height = template.length_mm, template.depth_mm
    if rotation == 90:
        width, height = height, width
    return box(x - width / 2, y - height / 2, x + width / 2, y + height / 2)


def _safe(footprint: Polygon, geometry: Geometry, clearance: float) -> bool:
    if not geometry.scope.covers(footprint):
        return False
    if footprint.distance(geometry.scope.boundary) + EPS_MM < clearance:
        return False
    if any(footprint.intersects(wall) or footprint.distance(wall) + EPS_MM < clearance
           for wall in geometry.walls):
        return False
    if any(footprint.intersects(zone) or footprint.distance(zone) + EPS_MM < clearance
           for zone in geometry.exclusions):
        return False
    # All racks are confined to one connected navigable part, without inventing an entrance.
    return geometry.navigation.covers(_nav_buffer(footprint))


def _make_bom(shelves: Iterable[Shelf], templates: list[Template]) -> list[BOMRow]:
    counts = Counter(s.material_id for s in shelves)
    return [BOMRow(material_id=t.material_id, length_mm=t.length_mm, depth_mm=t.depth_mm,
                   default_level_count=t.default_level_count, quantity=counts[t.material_id])
            for t in templates if counts[t.material_id]]


def validate_layout(package: UnderstandingPackage, result: LayoutResult) -> dict:
    """Independent full validation, including canonical rectangles and BOM identity."""
    geometry = _geometry(package)
    templates = _templates(package)
    lookup = {t.material_id: t for t in templates}
    if result.source != package.source or result.spatial != package.spatial:
        _fail("VALIDATION_FAILED", "方案的来源或空间数据与标准输入不一致。")
    if result.rules.get("aisle_mm") != AISLE_MM:
        _fail("VALIDATION_FAILED", "方案基础过道规则不是 1200 mm。")
    if not result.shelves or len({s.id for s in result.shelves}) != len(result.shelves):
        _fail("VALIDATION_FAILED", "货架为空或实例 ID 重复。")
    footprints = []
    minimum_clearance = math.inf
    for shelf in result.shelves:
        template = lookup.get(shelf.material_id)
        if template is None or (shelf.length_mm, shelf.depth_mm, shelf.default_level_count) != (
            template.length_mm, template.depth_mm, template.default_level_count
        ):
            _fail("VALIDATION_FAILED", f"货架 {shelf.id} 与物料模板不匹配。")
        if len(shelf.footprint_mm) < 4 or shelf.footprint_mm[0] != shelf.footprint_mm[-1]:
            _fail("VALIDATION_FAILED", f"货架 {shelf.id} 轮廓没有闭合。")
        polygon = Polygon(shelf.footprint_mm)
        expected = _footprint(shelf.x_mm, shelf.y_mm, template, shelf.rotation_deg)
        if not polygon.is_valid or not polygon.equals(expected):
            _fail("VALIDATION_FAILED", f"货架 {shelf.id} 轮廓与中心、旋转、规格不一致。")
        if not _safe(polygon, geometry, AISLE_MM):
            _fail("VALIDATION_FAILED", f"货架 {shelf.id} 越界、碰障碍或未满足基础过道规则。")
        minimum_clearance = min(minimum_clearance, polygon.distance(geometry.scope.boundary),
                                *(polygon.distance(o) for o in [*geometry.walls, *geometry.exclusions]))
        for previous in footprints:
            separation = polygon.distance(previous)
            if polygon.intersects(previous) or separation + EPS_MM < AISLE_MM:
                _fail("VALIDATION_FAILED", f"货架 {shelf.id} 与其他货架重叠或过道不足。")
            minimum_clearance = min(minimum_clearance, separation)
        footprints.append(polygon)
    if result.bom != _make_bom(result.shelves, templates):
        _fail("VALIDATION_FAILED", "物料清单的模板、规格、层数或数量与货架实例不一致。")
    # This is the centre space of a 1200 mm wide circular clearance envelope.
    # Use a geometric difference and connected-component check, not visual inspection.
    remaining = geometry.navigation.difference(unary_union([_nav_buffer(p) for p in footprints]))
    components = _parts(remaining)
    if len(components) != 1 or not remaining.is_valid:
        _fail("VALIDATION_FAILED", "货架布置切断了所选区域的 1200 mm 连通通道。")
    return {
        "status": "PASS", "shelf_count": len(footprints),
        "bom_quantity": sum(row.quantity for row in result.bom),
        "scope_and_holes": "PASS", "wall_clearance": "PASS",
        "exclusion_clearance": "PASS", "overlap": "PASS", "aisle_clearance": "PASS",
        "template_identity": "PASS", "bom_instance_identity": "PASS",
        "minimum_clearance_mm": round(minimum_clearance, 6),
        "navigation": {"status": "PASS", "width_mm": AISLE_MM,
                       "base_component_count": len(geometry.navigation_components),
                       "selected_component_rank": 1, "after_selected_component_count": 1,
                       "method": "scope inset and obstacle/shelf buffer with conservative chord-corrected 600 mm radius; one connected polygon",
                       "buffer_radius_mm": NAV_RADIUS_MM, "buffer_quad_segs": NAV_QUAD_SEGS,
                       "entrance_reachability": "UNKNOWN_NO_ENTRANCE_CONTRACT"},
    }


def plan_package(package: UnderstandingPackage | dict) -> LayoutResult:
    if isinstance(package, dict):
        package = UnderstandingPackage.model_validate(package)
    geometry = _geometry(package)
    templates = _templates(package)
    minx, miny, maxx, maxy = geometry.navigation.bounds
    candidates = []
    # Finite enumeration of homogeneous grids is deliberately simpler than a generic optimizer.
    for template in templates:
        for rotation in (0, 90):
            width, height = ((template.length_mm, template.depth_mm) if rotation == 0
                             else (template.depth_mm, template.length_mm))
            stepx, stepy = width + GRID_CLEARANCE_MM, height + GRID_CLEARANCE_MM
            columns = max(0, math.floor((maxx - minx) / stepx) + 1)
            rows = max(0, math.floor((maxy - miny) / stepy) + 1)
            if rows * columns > MAX_GRID_POINTS:
                _fail("PLANNING_LIMIT", "范围或物料尺寸导致候选网格超过 100000 点，请检查毫米单位和物料配置。")
            for phase_x, phase_y in ((0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (0.5, 0.5)):
                shelves = []
                for row in range(rows):
                    y = miny + GRID_CLEARANCE_MM - AISLE_MM / 2 + height / 2 + phase_y * stepy + row * stepy
                    for column in range(columns):
                        x = minx + GRID_CLEARANCE_MM - AISLE_MM / 2 + width / 2 + phase_x * stepx + column * stepx
                        footprint = _footprint(x, y, template, rotation)
                        if not _safe(footprint, geometry, GRID_CLEARANCE_MM):
                            continue
                        shelves.append(Shelf(id=f"S{len(shelves) + 1:04d}", material_id=template.material_id,
                            x_mm=x, y_mm=y, rotation_deg=rotation, length_mm=template.length_mm,
                            depth_mm=template.depth_mm, default_level_count=template.default_level_count,
                            footprint_mm=list(footprint.exterior.coords)))
                score = len(shelves) * template.length_mm * template.depth_mm * template.default_level_count
                candidates.append((score, len(shelves), template.material_id, rotation, phase_x, phase_y, shelves))
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2], c[3], c[4], c[5]))
    warnings = list(package.spatial.warnings) + [
        "基础过道参数为本项目 1200 mm 保守设计规则，不代表消防、无障碍或其他法规认证。",
        "货架显示高度 1800 mm 是可视化假定，不是物料采购规格；层数来自内部物料配置。",
        "未推测门店入口；连通性只验证内部所选通行区域，不代表已验证入口、出口或疏散可达性。",
        "没有按商品名称或规格推测包装尺寸；本方案不进行 SKU 上架、容量或 Facing 优化。",
    ]
    if len(geometry.navigation_components) > 1:
        warnings.append("原始空间存在多个满足基础宽度的通行分区；仅在面积最大的连通分区布架，其他分区保持空置。")
    rejected = 0
    for score, count, material_id, rotation, phase_x, phase_y, shelves in candidates:
        if not shelves:
            continue
        result = LayoutResult(source=package.source, spatial=package.spatial,
            rules={"aisle_mm": AISLE_MM, "candidate_clearance_mm": GRID_CLEARANCE_MM,
                   "display_shelf_height_mm": 1800, "display_height_is_assumption": True,
                   "clearance_policy": "each shelf to scope boundary, holes, walls, exclusions and other shelves >= aisle_mm"},
            shelves=shelves, bom=_make_bom(shelves, templates), validation={"status": "PENDING"},
            planning={"algorithm": "deterministic_island_grid_v1", "candidate_count": len(candidates),
                      "objective": "maximize nominal configured shelf-deck area, then instance count; geometric provision only, not SKU capacity",
                      "objective_nominal_deck_area_mm2": score,
                      "selected_template": material_id, "rotation_deg": rotation,
                      "grid_phase": [phase_x, phase_y], "rejected_by_final_validation": rejected,
                      "tie_break": "material_id ascending, rotation ascending, phase_x ascending, phase_y ascending"},
            business_summary={"product_count": package.business.product_count,
                "physical_dimensions_known": sum(all(isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool)
                    and math.isfinite(p[k]) and p[k] > 0 for k in ("width_mm", "depth_mm", "height_mm"))
                    for p in package.business.products)}, warnings=warnings)
        try:
            result.validation = validate_layout(package, result)
        except PlanningError:
            rejected += 1
            continue
        canonical = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        result.planning["deterministic_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        result.planning["hash_definition"] = "SHA256 of canonical sorted UTF-8 JSON before deterministic_sha256 and hash_definition are added"
        return result
    _fail("NO_SAFE_LAYOUT", "没有生成满足范围、孔洞、墙、禁放区、1200 mm 过道和连通性检查的安全货架方案。")
