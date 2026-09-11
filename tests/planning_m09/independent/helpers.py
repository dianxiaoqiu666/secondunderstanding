"""Independent physical checks and a frozen entrance ranking counterexample."""
from collections import Counter

import pytest
from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from services.planning import runs as baseline
from shared.planning_v2 import LayoutV2
from tests.audit_v2.test_obstacle_audit import check_physical_safety, end_corridors


def effective_length(layout):
    return sum(module.length_mm for run in layout.runs for module in run.modules)


def verify_instances(req, layout):
    check_physical_safety(req, layout)
    assert layout.source == req.source and layout.space == req.space and layout.rules == req.rules
    assert layout.shelves == [module for run in layout.runs for module in run.modules]
    templates = {t.material_id: t for t in req.business.templates}
    scope = Polygon(req.space.boundary.boundary_mm, req.space.boundary.holes_mm)
    quantities = Counter()
    total_levels = Counter()
    for run in layout.runs:
        polygons = []
        for module in run.modules:
            template = templates[module.material_id]
            assert (module.length_mm, module.depth_mm, module.default_level_count) == \
                (template.length_mm, template.depth_mm, template.default_level_count)
            w, h = (module.length_mm, module.depth_mm) if module.rotation_deg == 0 else (module.depth_mm, module.length_mm)
            expected = box(module.x_mm-w/2, module.y_mm-h/2, module.x_mm+w/2, module.y_mm+h/2)
            actual = Polygon(module.footprint_mm)
            assert module.footprint_mm[0] == module.footprint_mm[-1] and actual.is_valid
            assert actual.equals(expected), "The actual full footprint must match its displayed size, angle and center."
            polygons.append(actual)
            quantities[module.material_id] += 1
            total_levels[module.material_id] += module.default_level_count
        assert Polygon(run.footprint_mm).equals(unary_union(polygons))
        assert run.used_length_mm == pytest.approx(sum(m.length_mm for m in run.modules))
        for corridor in end_corridors(run, req.rules.end_aisle_mm):
            assert scope.covers(corridor)
    assert {row.material_id: row.quantity for row in layout.bom} == dict(quantities)
    assert {row.material_id: row.total_level_count for row in layout.bom} == dict(total_levels)
    for row in layout.bom:
        template = templates[row.material_id]
        assert (row.length_mm, row.depth_mm, row.default_level_count) == \
            (template.length_mm, template.depth_mm, template.default_level_count)


def entrance_average_trap(req, frozen_layout):
    """Remove the first row and shift the remaining valid frozen rows.

    This is a counterexample, never the expected optimized answer. Its source
    geometry, material catalog, clearances and module combinations are fixed.
    """
    trap = LayoutV2.model_validate(frozen_layout)
    assert all(run.direction_deg == 0 for run in trap.runs)
    rows = sorted({Polygon(run.footprint_mm).bounds[1] for run in trap.runs})
    trap.runs = [run for run in trap.runs if Polygon(run.footprint_mm).bounds[1] != rows[0]]
    depth = trap.runs[0].depth_mm
    target = min(y for _, y in req.space.boundary.boundary_mm) + req.rules.boundary_clearance_mm \
        + 0.75 * (depth + req.rules.aisle_width_mm)
    dy = target - rows[1]
    for run in trap.runs:
        run.start_mm = (run.start_mm[0], run.start_mm[1] + dy)
        run.end_mm = (run.end_mm[0], run.end_mm[1] + dy)
        run.footprint_mm = list(affinity.translate(Polygon(run.footprint_mm), yoff=dy).exterior.coords)
        for module in run.modules:
            module.y_mm += dy
            module.footprint_mm = list(affinity.translate(Polygon(module.footprint_mm), yoff=dy).exterior.coords)
    neighbors = baseline._geometric_neighbors(trap.runs)
    for run in trap.runs:
        run.neighbors = neighbors[run.run_id]
    trap.shelves = [module for run in trap.runs for module in run.modules]
    trap.bom = baseline._bom(trap.shelves, baseline._catalog(req.business.templates))
    trap.metrics = baseline._metrics(Polygon(req.space.boundary.boundary_mm, req.space.boundary.holes_mm), trap.runs)
    trap.validation = baseline.validate_runs(req, trap)
    verify_instances(req, trap)
    return trap
