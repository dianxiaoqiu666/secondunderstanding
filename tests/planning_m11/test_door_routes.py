"""Explicit outside-door counterfactuals; no frozen scene or holdout is opened."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import unittest

from shapely.geometry import LineString, Polygon

from shared.contracts import Wall
from shared.operations_planning import DoorConnection, OperationsInput, operations_digest, operations_request
from services.planning.operations_routes import (corridor_plans, evaluate_routes,
    validate_door_configuration, validate_route_evidence)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = runpy.run_path(str(ROOT / 'tests/planning_m10/test_routes.py'))


def explicit_door_fixture():
    value = FIXTURE['scenario']()
    value.physical_perimeter_walls = [Wall(id=f'ENTITY-{index}', start_mm=a, end_mm=b)
        for index, (a, b) in enumerate([
            ((0, 0), (14000, 0)), ((14000, 0), (14000, 10000)), ((14000, 10000), (0, 10000)),
            ((0, 10000), (0, 2600)), ((0, 1400), (0, 0))])]
    value.door_connection = DoorConnection(id='EXPLICIT-DOOR', inside_point_mm=(600, 2000),
        outside_point_mm=(-600, 2000), provenance='SYNTHETIC_TEST',
        connection_region={'boundary_mm': [(-600, 1400), (600, 1400), (600, 2600), (-600, 2600), (-600, 1400)]})
    value.workpoints.entrance.point_mm = (-600, 2000)
    return value, FIXTURE['layout_for'](value)


class ExplicitDoorRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.value, cls.layout = explicit_door_fixture()
        cls.result = evaluate_routes(cls.value, cls.layout)

    def test_actual_outside_origin_reaches_every_side_without_snapping(self):
        self.assertEqual(self.result['status'], 'PASS')
        self.assertTrue(self.result['full_operations_proven'])
        self.assertEqual(self.result['outside_entry_proof'], 'PASS')
        outside = self.value.workpoints.entrance.point_mm
        scope = Polygon(self.value.base.space.boundary.boundary_mm)
        for face in self.result['face_access']:
            self.assertEqual(tuple(face['entry_path_mm'][0]), outside)
            self.assertEqual(face['entry_path_mm'][:3], [(-600, 2000), (0, 2000), (600, 2000)])
            self.assertTrue(scope.covers(LineString(face['entry_path_mm'][2:])))
            self.assertTrue(face['reachable'])
        self.assertEqual({face['side'] for face in self.result['face_access'] if face['assembly_id'] == 'C1'}, {'A', 'B'})
        report = validate_route_evidence(self.value, self.layout, self.result)
        self.assertEqual(report['status'], 'PASS', report['issues'])
        self.assertEqual(report['entrance_connected_workpoint_count'], 4)
        self.assertTrue(report['full_operations_proven'])

    def test_sealing_same_door_entity_is_valid_input_but_blocks_outside_routes(self):
        closed = self.value.model_copy(deep=True)
        closed.physical_perimeter_walls.append(Wall(id='DOOR-CLOSED',
            start_mm=closed.entrance_opening_mm[0], end_mm=closed.entrance_opening_mm[1]))
        closed = OperationsInput.model_validate(closed.model_dump(mode='json'))
        self.assertEqual(validate_door_configuration(closed)['status'], 'PASS')
        result = evaluate_routes(closed, self.layout)
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['full_operations_proven'])
        self.assertEqual(result['outside_entry_proof'], 'FAIL')
        self.assertEqual(result['entrance_connection']['actual_clear_width_mm'], 0)
        self.assertTrue(all(not face['entrance']['reachable'] for face in result['face_access']))
        self.assertTrue(all(face['pick_start']['reachable'] for face in result['face_access']))
        self.assertEqual(len(result['picking_routes']), len(closed.demand.picking_tasks))
        self.assertIsNone(result['summary']['picking_mean_mm'])
        self.assertEqual(corridor_plans(closed, 0, 500), [])

    def test_outside_point_cannot_be_replaced_by_an_inside_or_other_point(self):
        for point in ((600, 2000), (-600, 2100)):
            with self.subTest(point=point):
                value = self.value.model_copy(deep=True)
                value.workpoints.entrance.point_mm = point
                self.assertEqual(validate_door_configuration(value)['code'], 'SCENARIO_CONFIGURATION_INVALID')
                result = evaluate_routes(value, self.layout)
                self.assertEqual(result['status'], 'FAIL')
                self.assertFalse(result['full_operations_proven'])
        value = self.value.model_copy(deep=True)
        value.workpoints.entrance.point_mm = value.door_connection.outside_point_mm = (600, 2000)
        self.assertEqual(validate_door_configuration(value)['code'], 'SCENARIO_CONFIGURATION_INVALID')

    def test_empty_physical_wall_evidence_does_not_turn_logical_scope_into_a_wall(self):
        value = self.value.model_copy(deep=True)
        value.physical_perimeter_walls = []
        result = evaluate_routes(value, self.layout)
        self.assertEqual(result['outside_entry_proof'], 'FAIL')
        self.assertFalse(result['full_operations_proven'])
        self.assertIn('EXPLICIT_PHYSICAL_PERIMETER_WALL_EVIDENCE_MISSING', result['entrance_connection']['diagnostics'])

    def test_external_link_cannot_bypass_a_blocked_door_through_other_boundary_gaps(self):
        value = self.value.model_copy(deep=True)
        value.physical_perimeter_walls.append(Wall(id='DOOR-CLOSED', start_mm=(0, 1400), end_mm=(0, 2600)))
        # Deleting a distant boundary entity does not create an exterior route
        # around the shop: no external navigation graph is built at all.
        value.physical_perimeter_walls = [wall for wall in value.physical_perimeter_walls if wall.id != 'ENTITY-2']
        result = evaluate_routes(value, self.layout)
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(all(not face['entrance']['reachable'] for face in result['face_access']))

    def test_connection_region_must_hold_the_configured_transverse_width(self):
        value = self.value.model_copy(deep=True)
        value.door_connection.connection_region.boundary_mm = [(-600, 1400.01), (600, 1400.01),
            (600, 2599.99), (-600, 2599.99), (-600, 1400.01)]
        result = evaluate_routes(value, self.layout)
        self.assertEqual(result['status'], 'FAIL')
        self.assertAlmostEqual(result['entrance_connection']['actual_clear_width_mm'], 1199.98)
        self.assertIsNone(result['minimum_route_clear_width_mm'])

    def test_corridor_reservations_stay_inside_and_use_explicit_inside_anchor(self):
        plans = corridor_plans(self.value, 0, 500)
        self.assertTrue(plans)
        scope = Polygon(self.value.base.space.boundary.boundary_mm)
        for plan in plans:
            for corridor in plan['corridors']:
                self.assertTrue(scope.covers(Polygon(corridor.boundary_mm, corridor.holes_mm)))
        # Extend only the explicitly allowed outside part; internal corridor
        # geometry and workpoint connectors must remain exactly unchanged.
        farther = self.value.model_copy(deep=True)
        farther.workpoints.entrance.point_mm = farther.door_connection.outside_point_mm = (-1200, 2000)
        farther.door_connection.connection_region.boundary_mm = [(-1200, 1400), (600, 1400),
            (600, 2600), (-1200, 2600), (-1200, 1400)]
        self.assertEqual(corridor_plans(farther, 0, 500), plans)
        result = evaluate_routes(farther, self.layout)
        self.assertEqual(result['status'], 'PASS')
        self.assertAlmostEqual(result['summary']['entry_to_face_mean_mm'] - self.result['summary']['entry_to_face_mean_mm'], 600)
        self.assertEqual(result['summary']['picking_mean_mm'], self.result['summary']['picking_mean_mm'])
        self.assertEqual(result['item_placements'], self.result['item_placements'])

    def test_actual_route_width_and_door_width_are_recomputed_and_tamper_rejected(self):
        self.assertEqual(self.result['minimum_route_clear_width_mm'], 1200)
        self.assertEqual(self.result['entrance_connection']['actual_clear_width_mm'], 1200)
        for key in ('route', 'door'):
            altered = deepcopy(self.result)
            if key == 'route':
                altered['minimum_route_clear_width_mm'] = altered['summary']['minimum_route_clear_width_mm'] = 1800
            else:
                altered['entrance_connection']['actual_clear_width_mm'] = 1800
            self.assertEqual(validate_route_evidence(self.value, self.layout, altered)['status'], 'FAIL')

    def test_compact_end_corridor_removes_only_double_counted_rack_setback(self):
        plans = corridor_plans(self.value, 0, 500)
        by_id = {plan['id']: plan for plan in plans}
        self.assertEqual(len(by_id), 6)
        usual = [Polygon(c.boundary_mm) for c in by_id['PERIMETER_LOOP']['corridors'] if c.role == 'END_AISLE']
        compact = [Polygon(c.boundary_mm) for c in by_id['COMPACT_PERIMETER_LOOP']['corridors'] if c.role == 'END_AISLE']
        self.assertEqual(min(p.bounds[0] for p in usual), 100)
        self.assertEqual(min(p.bounds[0] for p in compact), 0)
        self.assertTrue(all(p.bounds[2] - p.bounds[0] == 1200 for p in compact))
        fronts = [Polygon(c.boundary_mm) for c in by_id['WALL_FRONT_LOOP']['corridors']
                  if c.role in ('END_AISLE', 'WALL_FRONT_AISLE')]
        # Every side's forward passage leaves the 100+500 mm wall shelf band
        # available; the matching layout mechanism separately checks assemblies.
        self.assertEqual(min(p.bounds[0] for p in fronts), 600)
        self.assertEqual(min(p.bounds[1] for p in fronts), 600)
        value = self.value.model_copy(deep=True)
        value.base.rules.wall_clearance_mm = 300
        plans = corridor_plans(value, 0, 500)
        for plan in plans:
            fronts = [Polygon(c.boundary_mm) for c in plan['corridors'] if c.role == 'WALL_FRONT_AISLE']
            self.assertEqual(min(p.bounds[1] for p in fronts), 800)
            self.assertTrue(all(p.bounds[3] - p.bounds[1] == 1200 for p in fronts))

    def test_serialized_outside_witness_revalidates_and_physical_walls_are_not_cut(self):
        result = json.loads(json.dumps(self.result, allow_nan=False))
        self.assertEqual(validate_route_evidence(self.value, self.layout, result)['status'], 'PASS')
        before = self.value.model_dump(mode='json')
        request = operations_request(self.value)
        self.assertEqual(len(request.space.barriers), len(self.value.physical_perimeter_walls))
        self.assertEqual(self.value.model_dump(mode='json'), before)
        closed = self.value.model_copy(deep=True)
        closed.physical_perimeter_walls.append(Wall(id='DOOR-CLOSED', start_mm=(0, 1400), end_mm=(0, 2600)))
        request = operations_request(closed)
        self.assertTrue(any(wall.start_mm == (0, 1400) and wall.end_mm == (0, 2600) for wall in request.space.barriers))

    def test_legacy_digest_and_technical_routes_remain_explicitly_incomplete(self):
        value = FIXTURE['scenario']()
        old = value.model_dump(mode='json')
        old.pop('door_connection')
        old.pop('physical_perimeter_walls')
        old_hash = hashlib.sha256(json.dumps(old, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
        self.assertEqual(operations_digest(value), old_hash)
        result = evaluate_routes(value, FIXTURE['layout_for'](value))
        self.assertEqual(result['status'], 'PASS')
        self.assertFalse(result['full_operations_proven'])
        self.assertEqual(result['outside_entry_proof'], 'MISSING_LEGACY')
        fabricated = deepcopy(result)
        fabricated['full_operations_proven'] = True
        self.assertEqual(validate_route_evidence(value, FIXTURE['layout_for'](value), fabricated)['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
