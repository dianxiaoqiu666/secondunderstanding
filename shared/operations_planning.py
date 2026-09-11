"""Employee-only retail operations on explicit test geometry; no CAD inference."""
import hashlib
import json
from typing import Literal

from pydantic import Field, FiniteFloat, model_validator
from shared.contracts import Model, Point, PolygonData, Wall
from shared.explicit_planning import ExplicitPlanningInput, explicit_request
from shared.planning_v2 import LayoutV2


class UsableWall(Model):
    id: str
    start_mm: Point
    end_mm: Point
    inward_normal: Point
    provenance: Literal['SYNTHETIC_TEST', 'EXPLICIT_CONFIRMED']


class WorkPoint(Model):
    point_mm: Point
    provenance: Literal['SYNTHETIC_TEST'] = 'SYNTHETIC_TEST'


class WorkPoints(Model):
    entrance: WorkPoint
    receiving: WorkPoint
    pick_start: WorkPoint
    packing: WorkPoint


class TestItem(Model):
    id: str
    target_fraction: Point


class TestTask(Model):
    id: str
    item_ids: list[str] = Field(min_length=1)


class OperationsDemand(Model):
    provenance: Literal['SYNTHETIC_EVALUATION'] = 'SYNTHETIC_EVALUATION'
    items: list[TestItem] = Field(min_length=1)
    picking_tasks: list[TestTask] = Field(min_length=1)
    replenishment_tasks: list[TestTask] = Field(min_length=1)
    minimum_pick_length_mm: FiniteFloat = Field(gt=0)
    minimum_nominal_board_area_m2: FiniteFloat = Field(gt=0)
    placement_policy: Literal['FIXED_SPATIAL_TARGET_NEAREST_UNIQUE_FACE_V1'] = 'FIXED_SPATIAL_TARGET_NEAREST_UNIQUE_FACE_V1'
    route_policy: Literal['WEIGHTED_NEAREST_UNVISITED_THEN_DESTINATION_V1'] = 'WEIGHTED_NEAREST_UNVISITED_THEN_DESTINATION_V1'

    @model_validator(mode='after')
    def consistent_tasks(self):
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate synthetic item ID')
        for item in self.items:
            if not all(0 <= fraction <= 1 for fraction in item.target_fraction):
                raise ValueError('Synthetic placement target must be within the unit rectangle')
        for tasks in (self.picking_tasks, self.replenishment_tasks):
            if len({task.id for task in tasks}) != len(tasks):
                raise ValueError('Duplicate task ID')
            for task in tasks:
                if len(set(task.item_ids)) != len(task.item_ids) or not set(task.item_ids) <= set(ids):
                    raise ValueError('Each task must reference distinct known synthetic items')
        covered = {item for task in self.picking_tasks for item in task.item_ids}
        if covered != set(ids):
            raise ValueError('Every fixed synthetic item must participate in picking evaluation')
        if {item for task in self.replenishment_tasks for item in task.item_ids} != set(ids):
            raise ValueError('Every fixed synthetic item must participate in replenishment evaluation')
        return self


class AssemblyAssumptions(Model):
    structure: Literal['TWO_INDEPENDENT_SINGLE_SIDED_MODULES'] = 'TWO_INDEPENDENT_SINGLE_SIDED_MODULES'
    structure_gap_mm: FiniteFloat = Field(default=0, ge=0, le=300)
    provenance: Literal['TEST_ASSUMPTION'] = 'TEST_ASSUMPTION'
    net_shelf_dimensions_status: Literal['UNKNOWN'] = 'UNKNOWN'
    procurement_structure_status: Literal['UNKNOWN'] = 'UNKNOWN'


class DoorConnection(Model):
    """An explicit local outside/inside link; never an exterior navigation domain."""
    id: str = Field(min_length=1)
    inside_point_mm: Point
    outside_point_mm: Point
    connection_region: PolygonData
    provenance: Literal['SYNTHETIC_TEST', 'EXPLICIT_CONFIRMED']


class OperationsInput(Model):
    schema_version: Literal['employee-operations-1.0'] = 'employee-operations-1.0'
    input_kind: Literal['ALGORITHM_VALIDATION'] = 'ALGORITHM_VALIDATION'
    base: ExplicitPlanningInput
    usable_walls: list[UsableWall] = Field(default_factory=list)
    entrance_opening_mm: list[Point] = Field(min_length=2, max_length=2)
    physical_perimeter_walls: list[Wall] = Field(default_factory=list)
    door_connection: DoorConnection | None = None
    workpoints: WorkPoints
    assembly_assumptions: AssemblyAssumptions = Field(default_factory=AssemblyAssumptions)
    demand: OperationsDemand


class ShelfAssembly(Model):
    id: str
    kind: Literal['WALL_SINGLE', 'CENTRAL_SINGLE', 'BACK_TO_BACK', 'M09_SINGLE_ASSUMPTION']
    run_ids: list[str] = Field(min_length=1, max_length=2)
    footprint_mm: list[Point] = Field(min_length=4)
    side_depths_mm: list[FiniteFloat]
    total_depth_mm: FiniteFloat = Field(gt=0)
    structure_gap_mm: FiniteFloat = Field(ge=0)
    wall_id: str | None = None
    bay_pairs: list[list[str]] = Field(default_factory=list)
    material_basis: Literal['INDEPENDENT_MODULE_INSTANCES', 'M09_TEST_FACE_ASSUMPTION'] = 'INDEPENDENT_MODULE_INSTANCES'


class PickFace(Model):
    id: str
    module_id: str
    run_id: str
    assembly_id: str
    side: Literal['A', 'B', 'SINGLE']
    start_mm: Point
    end_mm: Point
    outward_normal: Point
    standing_point_mm: Point
    direction_basis: Literal['EXPLICIT_LAYOUT_TEST_ASSUMPTION'] = 'EXPLICIT_LAYOUT_TEST_ASSUMPTION'


class PlannedCorridor(PolygonData):
    id: str
    role: str


class OperationsLayout(LayoutV2):
    operational_schema_version: Literal['employee-layout-1.0'] = 'employee-layout-1.0'
    assemblies: list[ShelfAssembly] = Field(default_factory=list)
    pick_faces: list[PickFace] = Field(default_factory=list)
    planned_corridors: list[PlannedCorridor] = Field(default_factory=list)
    workpoints: WorkPoints
    route_evaluation: dict = Field(default_factory=dict)
    operational_policy: str


def operations_digest(value: OperationsInput):
    data = value.model_dump(mode='json')
    # Preserve the legacy input digest when neither additive door field is used.
    # The M11 explicit walls and complete door connection are otherwise bound.
    if value.door_connection is None and not value.physical_perimeter_walls:
        data.pop('door_connection', None)
        data.pop('physical_perimeter_walls', None)
    return hashlib.sha256(json.dumps(data, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def operations_request(value: OperationsInput):
    # All inherited geometry validation uses the normal explicit request. The
    # outer operations digest additionally binds workpoints, tasks and assumptions.
    if not value.physical_perimeter_walls:
        return explicit_request(value.base)
    base = value.base.model_copy(deep=True)
    # Entity walls remain solids even when the logical scope has an opening
    # annotation. Do not subtract the entrance from any supplied wall geometry.
    existing = {wall.id for wall in base.space.barriers}
    for wall in value.physical_perimeter_walls:
        copy = wall.model_copy(deep=True)
        copy.id = f'PHYSICAL-PERIMETER:{wall.id}'
        if copy.id in existing:
            raise ValueError('SCENARIO_CONFIGURATION_INVALID: duplicate physical wall identifier')
        existing.add(copy.id)
        base.space.barriers.append(copy)
    return explicit_request(base)
