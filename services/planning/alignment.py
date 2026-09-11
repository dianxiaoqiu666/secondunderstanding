"""Align shorter runs to adjacent longer runs within their existing intervals.

Only the along-run position changes. Row positions, depth, module composition,
available intervals and all original clearances remain fixed. This is a finite
candidate proposal; the caller must remeasure and validate the complete layout.
"""
from services.planning.runs import (
    EPS, _available_intervals, _geometric_neighbors, _rect, _safe_region, _xy,
)


def align_run_endpoints(req, scope, runs):
    """Return copied runs and the longer-neighbor anchors used to move them."""
    result = [run.model_copy(deep=True) for run in runs]
    by_id = {run.run_id: run for run in runs}
    neighbors = _geometric_neighbors(runs)
    region = _safe_region(req, scope)
    moves = []
    for run in result:
        axis = 0 if run.direction_deg == 0 else 1
        start = run.start_mm[axis]
        cross = run.start_mm[1 - axis]
        intervals = _available_intervals(
            req, scope, run.direction_deg, cross - run.depth_mm / 2, run.depth_mm, region)
        matching = [(low, high) for low, high in intervals
                    if low <= start + EPS and start + run.used_length_mm <= high + EPS
                    and abs(high - low - run.available_length_mm) <= EPS]
        if len(matching) != 1:
            continue  # No unambiguous existing interval to move inside.
        low, high = matching[0]
        anchors = []
        # All proposals read the original geometry, so iteration order cannot
        # feed one tentative move into another. Strictly longer anchors avoid
        # mutual drift among equal-length rows.
        for relation in neighbors[run.run_id]:
            other = by_id[relation['run_id']]
            if other.used_length_mm <= run.used_length_mm + EPS:
                continue
            for endpoint, target in (
                ('start', other.start_mm[axis]),
                ('end', other.end_mm[axis] - run.used_length_mm),
            ):
                if low <= target and target + run.used_length_mm <= high:
                    anchors.append((-other.used_length_mm, abs(target - start),
                                    other.run_id, endpoint, target))
        if not anchors:
            continue
        _, _, anchor_id, endpoint, target = min(anchors)
        if abs(target - start) <= EPS:
            continue
        cursor = target
        cross_start = cross - run.depth_mm / 2
        for module in run.modules:
            module.x_mm, module.y_mm = _xy(
                cursor + module.length_mm / 2, cross, run.direction_deg)
            module.footprint_mm = list(_rect(
                cursor, cross_start, module.length_mm, run.depth_mm, run.direction_deg).exterior.coords)
            cursor += module.length_mm
        run.start_mm = _xy(target, cross, run.direction_deg)
        run.end_mm = _xy(cursor, cross, run.direction_deg)
        run.footprint_mm = list(_rect(
            target, cross_start, run.used_length_mm, run.depth_mm, run.direction_deg).exterior.coords)
        moves.append({'run_id': run.run_id, 'anchor_run_id': anchor_id,
                      'anchor_endpoint': endpoint, 'shift_along_run_mm': target - start})
    if moves:
        refreshed = _geometric_neighbors(result)
        for run in result:
            run.neighbors = refreshed[run.run_id]
    return result, moves
