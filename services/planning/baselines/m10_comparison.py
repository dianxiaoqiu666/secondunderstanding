"""Frozen M10 generator/selection, measured with the current explicit-door graph.

M10 had an inside entrance workpoint. Only its corridor-construction adapter
uses the explicitly supplied inside point; the returned layouts and all route
measurements retain the same complete corrected input used by M11.
"""
from services.planning.baselines.m10_corridors import corridor_plans as frozen_corridors
from services.planning.engine import PlanningError


def corridor_plans(value, direction, depth):
    planning_value=value.model_copy(deep=True)
    if value.door_connection is not None:
        planning_value.workpoints.entrance.point_mm=value.door_connection.inside_point_mm
    return frozen_corridors(planning_value,direction,depth)


def compare_m10(value):
    # Delayed import keeps the frozen selector's corridor dependency acyclic.
    from services.planning.baselines.m10_selection import compare_operations
    from services.planning.operations import measure_operations
    from shared.operations_planning import OperationsLayout, operations_digest
    try:
        result=compare_operations(value)
        layout=OperationsLayout.model_validate(result['after']['layout'])
        return dict(status='AVAILABLE',layout=result['after']['layout'],
                    measurements=measure_operations(value,layout,layout.route_evaluation),elapsed_ms=result['after']['elapsed_ms'],
                    selection=result['selection'],input_sha256=operations_digest(value),
                    algorithm='M10_FROZEN_WITH_CURRENT_DOOR_ROUTE_MEASUREMENT',
                    comparison_note='M10 生成与选优冻结；门外连接使用与 M11 相同的修正输入及路由度量。')
    except PlanningError as error:
        return dict(status='NOT_FOUND',layout=None,measurements=None,elapsed_ms=None,
                    error={'code':error.code,'message':error.message},
                    input_sha256=operations_digest(value),algorithm='M10_FROZEN_WITH_CURRENT_DOOR_ROUTE_MEASUREMENT')
