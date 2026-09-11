"""Independent route witnesses, exact-width passages, and fixed-task integrity."""
from copy import deepcopy
import json
import unittest

from shapely.geometry import Polygon, box

from shared.contracts import Shelf, Wall
from shared.operations_planning import OperationsInput, OperationsLayout, PickFace, ShelfAssembly, operations_request
from shared.planning_v2 import BOMRowV2, ShelfRun
from services.planning.operations_routes import corridor_plans, evaluate_routes, validate_route_evidence


def scenario():
    return OperationsInput.model_validate({
        'base': {'input_kind': 'ALGORITHM_VALIDATION', 'name': 'route-tests',
                 'space': {'boundary': {'boundary_mm': [[0, 0], [14000, 0], [14000, 10000], [0, 10000], [0, 0]]}},
                 'templates': [{'material_id': 'T', 'length_mm': 2000, 'depth_mm': 500, 'default_level_count': 5}]},
        'entrance_opening_mm': [[0, 1400], [0, 2600]],
        'workpoints': {role: {'point_mm': point} for role, point in {
            'entrance': [600, 2000], 'receiving': [600, 3000],
            'pick_start': [600, 2000], 'packing': [600, 4000]}.items()},
        'demand': {
            'items': [{'id': f'I{index}', 'target_fraction': point} for index, point in enumerate(
                [[.2, .1], [.5, .1], [.8, .1], [.2, .8], [.5, .8], [.8, .8]])],
            'picking_tasks': [{'id': 'P1', 'item_ids': ['I0', 'I1', 'I2']},
                              {'id': 'P2', 'item_ids': ['I3', 'I4', 'I5']},
                              {'id': 'P3', 'item_ids': ['I0', 'I5']}],
            'replenishment_tasks': [{'id': 'R1', 'item_ids': ['I0', 'I3', 'I5']},
                                   {'id': 'R2', 'item_ids': ['I1', 'I2', 'I4']}],
            'minimum_pick_length_mm': 12000, 'minimum_nominal_board_area_m2': 30}})


def layout_for(value, central_bottom=1800, gap=0):
    runs, shelves, assemblies, faces = [], [], [], []
    def run(identifier, bottom, normal, side, assembly_id):
        modules = []
        for index in range(4):
            low, high = 2000 + 2000 * index, 4000 + 2000 * index
            polygon = box(low, bottom, high, bottom + 500)
            module = Shelf(id=f'{identifier}-{index}', material_id='T', x_mm=(low + high) / 2,
                           y_mm=bottom + 250, rotation_deg=0, length_mm=2000, depth_mm=500,
                           default_level_count=5, footprint_mm=list(polygon.exterior.coords))
            modules.append(module)
            face_y = bottom + 500 if normal > 0 else bottom
            faces.append(PickFace(id=f'F-{module.id}', module_id=module.id, run_id=identifier,
                                 assembly_id=assembly_id, side=side, start_mm=(low, face_y),
                                 end_mm=(high, face_y), outward_normal=(0, normal),
                                 standing_point_mm=((low + high) / 2, face_y + 600 * normal)))
        shelves.extend(modules)
        polygon = box(2000, bottom, 10000, bottom + 500)
        runs.append(ShelfRun(run_id=identifier, direction_deg=0, start_mm=(2000, bottom),
                            end_mm=(10000, bottom), available_length_mm=8000, depth_mm=500,
                            modules=modules, used_length_mm=8000, remaining_length_mm=0,
                            footprint_mm=list(polygon.exterior.coords)))
    run('wall-low', 100, 1, 'SINGLE', 'W1')
    run('wall-high', 9400, -1, 'SINGLE', 'W2')
    run('pair-low', central_bottom, -1, 'A', 'C1')
    run('pair-high', central_bottom + 500 + gap, 1, 'B', 'C1')
    for identifier, run_id in [('W1', 'wall-low'), ('W2', 'wall-high')]:
        item = next(item for item in runs if item.run_id == run_id)
        assemblies.append(ShelfAssembly(id=identifier, kind='WALL_SINGLE', run_ids=[run_id],
                                        footprint_mm=item.footprint_mm, side_depths_mm=[500],
                                        total_depth_mm=500, structure_gap_mm=0))
    assemblies.append(ShelfAssembly(id='C1', kind='BACK_TO_BACK', run_ids=['pair-low', 'pair-high'],
                                    footprint_mm=list(box(2000, central_bottom, 10000,
                                                         central_bottom + 1000 + gap).exterior.coords),
                                    side_depths_mm=[500, 500], total_depth_mm=1000 + gap,
                                    structure_gap_mm=gap,
                                    bay_pairs=[[f'pair-low-{i}', f'pair-high-{i}'] for i in range(4)]))
    request = operations_request(value)
    return OperationsLayout(source=request.source, space=request.space, rules=request.rules,
                            runs=runs, shelves=shelves, assemblies=assemblies, pick_faces=faces,
                            bom=[BOMRowV2(material_id='T', length_mm=2000, depth_mm=500,
                                          default_level_count=5, quantity=16, total_level_count=80)],
                            metrics={}, validation={}, workpoints=value.workpoints,
                            operational_policy='INDEPENDENT_ROUTE_TEST')


class RouteEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.value = scenario()
        cls.layout = layout_for(cls.value)
        cls.result = evaluate_routes(cls.value, cls.layout)

    def test_exact_rule_width_and_both_assembly_sides_are_proven(self):
        self.assertEqual(self.result['status'], 'PASS')
        self.assertEqual(self.result['summary']['reachable_face_count'], 16)
        self.assertEqual({face['side'] for face in self.result['face_access']
                          if face['assembly_id'] == 'C1'}, {'A', 'B'})
        self.assertEqual(validate_route_evidence(self.value, self.layout, self.result)['status'], 'PASS')
        # The 1200 mm lower aisle has no area after two closed 600 mm buffers.
        # Its exact centre line must nevertheless appear as a legitimate witness.
        lower_faces = [face for face in self.result['face_access'] if face['module_id'].startswith('pair-low')]
        self.assertTrue(all(face['standing_point_mm'][1] == 1200 for face in lower_faces))
        self.assertTrue(self.result['walkable_polygons'])

    def test_multi_item_route_visits_every_fixed_position_once_in_policy(self):
        placement = {item['item_id']: item for item in self.result['item_placements']}
        self.assertEqual(len({item['face_id'] for item in placement.values()}), 6)
        for task in self.result['picking_routes'] + self.result['replenishment_routes']:
            self.assertEqual(set(task['item_ids']), set(task['visited_item_ids']))
            self.assertEqual(len(task['item_ids']), len(task['visited_item_ids']))
            self.assertAlmostEqual(task['distance_mm'], sum(task['leg_distances_mm']))
            self.assertGreater(task['distance_mm'], len(task['path_mm']))
        single_returns = sum(next(face for face in self.result['face_access'] if face['face_id'] ==
            placement[item]['face_id'])['pick_start_distance_mm'] * 2 for item in ['I0', 'I1', 'I2'])
        self.assertLess(self.result['picking_routes'][0]['distance_mm'], single_returns)

    def test_json_witness_is_serializable_and_repeatable(self):
        encoded = json.dumps(self.result, allow_nan=False, sort_keys=True)
        self.assertEqual(encoded, json.dumps(evaluate_routes(self.value, self.layout), allow_nan=False, sort_keys=True))
        self.assertEqual(validate_route_evidence(self.value, self.layout, json.loads(encoded))['status'], 'PASS')

    def test_tampered_distance_and_wall_crossing_are_rejected(self):
        tampered = deepcopy(self.result)
        tampered['picking_routes'][0]['distance_mm'] = 1
        self.assertIn('PATH_DISTANCE_NOT_ACTUAL_LENGTH', {item['code'] for item in
            validate_route_evidence(self.value, self.layout, tampered)['issues']})
        tampered = deepcopy(self.result)
        tampered['face_access'][0]['receiving']['path_mm'] = [[600, 3000], [-100, 3000],
            tampered['face_access'][0]['standing_point_mm']]
        self.assertIn('PATH_CLEARANCE_OR_OBSTACLE_VIOLATION', {item['code'] for item in
            validate_route_evidence(self.value, self.layout, tampered)['issues']})

    def test_tampered_order_and_summary_cannot_claim_better_fixed_tasks(self):
        tampered = deepcopy(self.result)
        tampered['picking_routes'][0]['visited_item_ids'].reverse()
        self.assertIn('TASK_DETERMINISTIC_VISIT_POLICY_MISMATCH', {item['code'] for item in
            validate_route_evidence(self.value, self.layout, tampered)['issues']})
        tampered = deepcopy(self.result)
        tampered['summary']['picking_mean_mm'] = 0
        self.assertIn('FIXED_TASK_SUMMARY_MISMATCH', {item['code'] for item in
            validate_route_evidence(self.value, self.layout, tampered)['issues']})

    def test_missing_counted_module_face_and_subrule_gap_are_not_accepted(self):
        missing = self.layout.model_copy(deep=True)
        missing.pick_faces.pop()
        result = evaluate_routes(self.value, missing)
        self.assertEqual(result['status'], 'FAIL')
        self.assertIsNone(result['summary']['picking_mean_mm'])
        too_narrow = layout_for(self.value, central_bottom=1799.999)
        result = evaluate_routes(self.value, too_narrow)
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(any(not face['reachable'] for face in result['face_access']
                            if face['module_id'].startswith('pair-low')))

    def test_scope_hole_is_kept_out_of_every_real_path(self):
        value = self.value.model_copy(deep=True)
        value.base.space.boundary.holes_mm = [[(1000, 5000), (1500, 5000), (1500, 8000),
                                               (1000, 8000), (1000, 5000)]]
        result = evaluate_routes(value, self.layout)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(validate_route_evidence(value, self.layout, result)['status'], 'PASS')
        from shapely.geometry import LineString
        hole = Polygon(value.base.space.boundary.holes_mm[0])
        for route in result['picking_routes'] + result['replenishment_routes']:
            self.assertGreaterEqual(LineString(route['path_mm']).distance(hole) + 1e-6, 600)

    def test_rotated_shelves_and_entrance_preserve_real_distance_units(self):
        value, layout = self.value.model_copy(deep=True), self.layout.model_copy(deep=True)
        def transpose(point):
            return (point[1], point[0])
        value.base.space.boundary.boundary_mm = list(map(transpose, value.base.space.boundary.boundary_mm))
        value.entrance_opening_mm = list(map(transpose, value.entrance_opening_mm))
        for role in ('entrance', 'receiving', 'pick_start', 'packing'):
            getattr(value.workpoints, role).point_mm = transpose(getattr(value.workpoints, role).point_mm)
        for item in value.demand.items:
            item.target_fraction = transpose(item.target_fraction)
        for shelf in layout.shelves:
            shelf.x_mm, shelf.y_mm = shelf.y_mm, shelf.x_mm
            shelf.rotation_deg = 90
            shelf.footprint_mm = list(map(transpose, shelf.footprint_mm))
        for run in layout.runs:
            run.direction_deg = 90
            run.start_mm, run.end_mm = transpose(run.start_mm), transpose(run.end_mm)
            run.footprint_mm = list(map(transpose, run.footprint_mm))
        for assembly in layout.assemblies:
            assembly.footprint_mm = list(map(transpose, assembly.footprint_mm))
        for face in layout.pick_faces:
            face.start_mm, face.end_mm = transpose(face.start_mm), transpose(face.end_mm)
            face.outward_normal = transpose(face.outward_normal)
            face.standing_point_mm = transpose(face.standing_point_mm)
        result = evaluate_routes(value, layout)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(validate_route_evidence(value, layout, result)['status'], 'PASS')
        for key in ('picking_mean_mm', 'picking_worst_mm', 'replenishment_mean_mm', 'replenishment_worst_mm'):
            self.assertAlmostEqual(result['summary'][key], self.result['summary'][key])
        self.assertEqual(len(corridor_plans(value, 90, 500)), 2)

    def test_unreachable_face_keeps_all_tasks_and_nulls_comparable_metrics(self):
        value = self.value.model_copy(deep=True)
        value.base.space.barriers = [Wall(id='separating-wall', start_mm=(1200, 0), end_mm=(1200, 10000))]
        value = OperationsInput.model_validate(value.model_dump(mode='json'))
        result = evaluate_routes(value, self.layout)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(len(result['picking_routes']), len(value.demand.picking_tasks))
        self.assertEqual(len(result['item_placements']), len(value.demand.items))
        self.assertIsNone(result['summary']['picking_mean_mm'])
        self.assertIsNone(result['summary']['replenishment_mean_mm'])
        self.assertEqual([item['face_id'] for item in result['item_placements']],
                         [item['face_id'] for item in self.result['item_placements']])

    def test_back_to_back_structure_gap_is_never_a_passage(self):
        layout = layout_for(self.value, central_bottom=3000, gap=2000)
        face = next(face for face in layout.pick_faces if face.module_id == 'pair-low-0')
        face.start_mm, face.end_mm = (2000, 3500), (4000, 3500)
        face.outward_normal, face.standing_point_mm = (0, 1), (3000, 4100)
        result = evaluate_routes(self.value, layout)
        self.assertEqual(result['status'], 'FAIL')
        evidence = next(item for item in result['face_access'] if item['face_id'] == face.id)
        self.assertFalse(evidence['reachable'])
        self.assertIn('FACE_NOT_ON_MODULE_AND_ASSEMBLY_EXTERIOR', evidence['geometry_issues'])
        self.assertFalse(evidence['receiving']['reachable'])

    def test_opening_width_and_explicit_wall_attachment_are_required(self):
        value = self.value.model_copy(deep=True)
        value.entrance_opening_mm = [(0, 1400), (0, 2599)]
        self.assertEqual(corridor_plans(value, 0, 500), [])
        result = evaluate_routes(value, self.layout)
        self.assertEqual(result['entrance_connection']['status'], 'FAIL')
        self.assertIsNone(result['summary']['picking_mean_mm'])
        value.entrance_opening_mm = [(100, 1400), (100, 2600)]
        self.assertEqual(corridor_plans(value, 0, 500), [])

    def test_corridors_precede_shelves_and_include_all_workpoints(self):
        plans = corridor_plans(self.value, 0, 500)
        self.assertEqual([item['id'] for item in plans],
                         ['PERIMETER_LOOP', 'PERIMETER_LOOP_WITH_ENTRY_CROSS_LINK'])
        for plan in plans:
            polygons = [Polygon(item.boundary_mm, item.holes_mm) for item in plan['corridors']]
            self.assertTrue(polygons)
            self.assertTrue(any(item.role == 'ENTRANCE_ACCESS' for item in plan['corridors']))
            from shapely.geometry import Point
            for role in ('entrance', 'receiving', 'pick_start', 'packing'):
                point = Point(getattr(self.value.workpoints, role).point_mm)
                self.assertTrue(any(polygon.covers(point) for polygon in polygons))

    def test_entrance_change_recomputes_distance_without_reassigning_items(self):
        value = self.value.model_copy(deep=True)
        value.entrance_opening_mm = [(14000, 1400), (14000, 2600)]
        value.workpoints.entrance.point_mm = (13400, 2000)
        changed = evaluate_routes(value, self.layout)
        self.assertEqual(changed['status'], 'PASS')
        self.assertNotEqual(changed['summary']['entry_to_face_mean_mm'],
                            self.result['summary']['entry_to_face_mean_mm'])
        self.assertEqual(changed['item_placements'], self.result['item_placements'])


if __name__ == '__main__':
    unittest.main()
