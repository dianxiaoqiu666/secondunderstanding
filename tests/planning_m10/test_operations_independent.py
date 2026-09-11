"""Adversarial integration review; reads only the frozen main short-entry input.

The holdout is neither generated nor evaluated. No report, baseline, CAD, or
service state is written by this suite. All corruptions are in-memory copies.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from shapely.geometry import Polygon, box

from shared.contracts import Exclusion, Wall
from shared.operations_planning import OperationsInput, operations_request
from services.planning.engine import PlanningError
import services.planning.operations as operations_module
from services.planning.operations import _seal, compare_operations, measure_operations
from services.planning.operations_layouts import generate_candidate, geometry_metrics, validate_operations_geometry
from services.planning.operations_routes import corridor_plans, evaluate_routes, validate_route_evidence
from services.planning.runs import _geometric_neighbors

ROOT = Path(__file__).resolve().parents[2]


class IndependentOperationsReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frozen = json.loads((ROOT / 'tools/benchmarks/scenarios_m10.json').read_text(encoding='utf-8-sig'))
        record = next(row for row in frozen['scenarios'] if row['id'] == 'op-short')
        assert record['group'] == 'main', 'This suite must never tune or run the holdout'
        cls.value = OperationsInput.model_validate(record['input'])
        plan = next(plan for plan in corridor_plans(cls.value, 0, 400)
                    if plan['id'] == 'PERIMETER_LOOP_WITH_ENTRY_CROSS_LINK')
        cls.layout = generate_candidate(cls.value, 0, 400, plan['corridors'], 'BACK_TO_BACK', 'INDEPENDENT_MAIN_FIXTURE')
        cls.evaluation = evaluate_routes(cls.value, cls.layout)
        assert cls.evaluation['status'] == 'PASS'

    def reject_geometry(self, layout, value=None):
        with self.assertRaises((PlanningError, ValueError)):
            validate_operations_geometry(value or self.value, layout)

    @staticmethod
    def rebind(value, layout):
        request = operations_request(value)
        layout.source, layout.space, layout.rules = request.source, request.space, request.rules
        layout.workpoints = value.workpoints

    def test_actual_main_fixture_has_distinct_modules_bays_runs_and_all_faces(self):
        result = validate_operations_geometry(self.value, self.layout)
        self.assertEqual(result['status'], 'PASS')
        paired = [assembly for assembly in self.layout.assemblies if assembly.kind == 'BACK_TO_BACK']
        walls = [assembly for assembly in self.layout.assemblies if assembly.kind == 'WALL_SINGLE']
        self.assertTrue(paired and walls)
        pair_modules = sum(len(pair) for assembly in paired for pair in assembly.bay_pairs)
        wall_modules = sum(len(run.modules) for run in self.layout.runs
                           if any(run.run_id in assembly.run_ids for assembly in walls))
        self.assertEqual(pair_modules + wall_modules, len(self.layout.shelves))
        self.assertEqual(sum(row.quantity for row in self.layout.bom), len(self.layout.shelves))
        self.assertEqual(len(self.layout.pick_faces), len(self.layout.shelves))
        evidence = validate_route_evidence(self.value, self.layout, self.evaluation)
        self.assertEqual(evidence['status'], 'PASS', evidence['issues'])
        self.assertEqual(evidence['entrance_connected_workpoint_count'], 4)

    def test_pair_footprint_structure_depth_and_bay_count_cannot_be_forged(self):
        for attack in ('footprint', 'depth', 'gap', 'pair_omission', 'single_label', 'shared_material'):
            with self.subTest(attack=attack):
                candidate = self.layout.model_copy(deep=True)
                assembly = next(assembly for assembly in candidate.assemblies if assembly.kind == 'BACK_TO_BACK')
                if attack == 'footprint':
                    assembly.footprint_mm = next(run for run in candidate.runs if run.run_id == assembly.run_ids[0]).footprint_mm
                elif attack == 'depth':
                    assembly.total_depth_mm /= 2
                elif attack == 'gap':
                    assembly.structure_gap_mm = 100
                elif attack == 'pair_omission':
                    assembly.bay_pairs.pop()
                elif attack == 'single_label':
                    assembly.kind = 'CENTRAL_SINGLE'
                else:
                    assembly.material_basis = 'M09_TEST_FACE_ASSUMPTION'
                self.reject_geometry(candidate)

    def test_bom_and_nominal_space_cannot_stand_in_for_procurement_capacity(self):
        for attack in ('quantity', 'levels', 'nominal_area', 'fake_net_area'):
            with self.subTest(attack=attack):
                candidate = self.layout.model_copy(deep=True)
                if attack == 'quantity':
                    candidate.bom[0].quantity = max(1, candidate.bom[0].quantity // 2)
                elif attack == 'levels':
                    candidate.bom[0].total_level_count += 1
                elif attack == 'nominal_area':
                    candidate.metrics['nominal_board_area_m2'] += 1
                else:
                    candidate.metrics['net_shelf_area_m2'] = candidate.metrics['nominal_board_area_m2']
                    candidate.metrics['net_shelf_area_status'] = 'VERIFIED'
                self.reject_geometry(candidate)

    def test_removed_or_inward_back_face_cannot_count_as_reachable(self):
        for attack in ('missing_b_side', 'inward_normal', 'wrong_standing_point', 'wrong_wall'):
            with self.subTest(attack=attack):
                candidate = self.layout.model_copy(deep=True)
                if attack == 'missing_b_side':
                    candidate.pick_faces = [face for face in candidate.pick_faces if face.side != 'B']
                elif attack == 'wrong_wall':
                    next(assembly for assembly in candidate.assemblies if assembly.kind == 'WALL_SINGLE').wall_id = 'unknown'
                else:
                    face = next(face for face in candidate.pick_faces if face.side == 'A')
                    if attack == 'inward_normal':
                        face.outward_normal = tuple(-coordinate for coordinate in face.outward_normal)
                    else:
                        face.standing_point_mm = ((face.start_mm[0] + face.end_mm[0]) / 2,
                                                  (face.start_mm[1] + face.end_mm[1]) / 2)
                self.reject_geometry(candidate)

    def test_source_rule_and_workpoint_binding_prevents_quieter_rule_changes(self):
        for attack in ('aisle', 'end_aisle', 'workpoint', 'source'):
            with self.subTest(attack=attack):
                candidate = self.layout.model_copy(deep=True)
                if attack == 'aisle':
                    candidate.rules.aisle_width_mm -= 1
                elif attack == 'end_aisle':
                    candidate.rules.end_aisle_mm -= 1
                elif attack == 'workpoint':
                    candidate.workpoints.packing.point_mm = (1000, 1000)
                else:
                    candidate.source.sha256 = '0' * 64
                self.reject_geometry(candidate)

    def test_real_hole_wall_and_reservation_survive_digest_rebinding(self):
        # Rebind source and space to avoid merely testing the digest mismatch;
        # the unchanged rack must be rejected against the actual new geometry.
        for attack in ('hole', 'wall', 'reservation'):
            with self.subTest(attack=attack):
                value, candidate = self.value.model_copy(deep=True), self.layout.model_copy(deep=True)
                module = next(shelf for shelf in candidate.shelves if shelf.id.startswith('R0003'))
                x, y = module.x_mm, module.y_mm
                obstacle = list(box(x - 50, y - 50, x + 50, y + 50).exterior.coords)
                if attack == 'hole':
                    value.base.space.boundary.holes_mm.append(obstacle)
                elif attack == 'wall':
                    value.base.space.barriers.append(Wall(id='TEST-WALL', start_mm=(x, y - 50), end_mm=(x, y + 50)))
                else:
                    value.base.space.reserved_passages.append(Exclusion(id='TEST-RESERVATION', boundary_mm=obstacle))
                self.rebind(value, candidate)
                self.reject_geometry(candidate, value)

    def test_subrule_aisle_fails_even_after_consistent_geometry_and_metrics_edit(self):
        candidate = self.layout.model_copy(deep=True)
        assembly = next(assembly for assembly in candidate.assemblies
                        if assembly.kind == 'BACK_TO_BACK' and Polygon(assembly.footprint_mm).bounds[1] == 4600)
        shift = -.001
        def moved(points):
            return [(x, y + shift) for x, y in points]
        assembly.footprint_mm = moved(assembly.footprint_mm)
        run_ids = set(assembly.run_ids)
        module_ids = {module.id for run in candidate.runs if run.run_id in run_ids for module in run.modules}
        for run in candidate.runs:
            if run.run_id in run_ids:
                run.start_mm = moved([run.start_mm])[0]
                run.end_mm = moved([run.end_mm])[0]
                run.footprint_mm = moved(run.footprint_mm)
                for shelf in run.modules:
                    shelf.y_mm += shift
                    shelf.footprint_mm = moved(shelf.footprint_mm)
        # Reconstruct the flattened instances, so identity checks alone cannot
        # explain the failure; the assembly spacing has to reject this layout.
        candidate.shelves = [module.model_copy(deep=True) for run in candidate.runs for module in run.modules]
        for face in candidate.pick_faces:
            if face.module_id in module_ids:
                face.start_mm = moved([face.start_mm])[0]
                face.end_mm = moved([face.end_mm])[0]
                face.standing_point_mm = moved([face.standing_point_mm])[0]
        neighbors = _geometric_neighbors(candidate.runs)
        for run in candidate.runs:
            run.neighbors = neighbors[run.run_id]
        scope = Polygon(self.value.base.space.boundary.boundary_mm, self.value.base.space.boundary.holes_mm)
        candidate.metrics = geometry_metrics(scope, candidate.runs, candidate.assemblies)
        with self.assertRaisesRegex(PlanningError, '通行净距不足'):
            validate_operations_geometry(self.value, candidate)

    def test_fail_or_partial_evaluation_cannot_be_sealed(self):
        for attack in ('status', 'complete_marker', 'entry_average'):
            with self.subTest(attack=attack):
                evaluation = deepcopy(self.evaluation)
                if attack == 'status':
                    evaluation['status'] = 'FAIL'
                    for key in ('picking_mean_mm', 'picking_worst_mm', 'replenishment_mean_mm', 'replenishment_worst_mm'):
                        evaluation['summary'][key] = None
                elif attack == 'complete_marker':
                    evaluation['summary']['complete_fixed_tasks'] = False
                else:
                    evaluation['summary']['entry_to_face_mean_mm'] = 0
                self.assertEqual(validate_route_evidence(self.value, self.layout, evaluation)['status'], 'FAIL')
                with self.assertRaises(PlanningError):
                    _seal(self.value, self.layout, evaluation)

    def test_displayed_graph_entrance_diagnostics_and_shared_segments_are_recomputed(self):
        for attack in ('entry_width', 'far_diagnostic', 'shared_segment', 'walkable', 'weight_unit', 'face_identity'):
            with self.subTest(attack=attack):
                evaluation = deepcopy(self.evaluation)
                if attack == 'entry_width':
                    evaluation['entrance_connection']['opening_width_mm'] *= 2
                elif attack == 'far_diagnostic':
                    evaluation['summary']['farthest_faces'][0]['distance_mm'] = 0
                elif attack == 'shared_segment':
                    evaluation['shared_segments'][0]['length_mm'] = 0
                elif attack == 'walkable':
                    evaluation['walkable_polygons'] = []
                elif attack == 'weight_unit':
                    evaluation['graph']['weight_unit'] = 'edge_count'
                else:
                    evaluation['face_access'][0]['module_id'] = 'wrong-module'
                result = validate_route_evidence(self.value, self.layout, evaluation)
                self.assertEqual(result['status'], 'FAIL', attack)

    def test_fixed_tasks_and_items_cannot_be_deleted_to_improve_mean(self):
        evaluation = deepcopy(self.evaluation)
        evaluation['picking_routes'].pop()
        kept = [route['distance_mm'] for route in evaluation['picking_routes']]
        evaluation['summary']['picking_mean_mm'] = sum(kept) / len(kept)
        evaluation['summary']['picking_worst_mm'] = max(kept)
        report = validate_route_evidence(self.value, self.layout, evaluation)
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('FIXED_TASK_COVERAGE_MISMATCH', {issue['code'] for issue in report['issues']})
        evaluation = deepcopy(self.evaluation)
        evaluation['item_placements'].pop()
        self.assertEqual(validate_route_evidence(self.value, self.layout, evaluation)['status'], 'FAIL')

    def test_correct_task_total_cannot_hide_false_per_leg_witnesses(self):
        evaluation = deepcopy(self.evaluation)
        route = evaluation['picking_routes'][0]
        # Keep the complete actual route and its total distance unchanged but
        # move one edge across the first/second leg boundary. A verifier that
        # only adds the whole polyline would falsely certify the task labels.
        self.assertGreater(len(route['legs'][0]['path_mm']), 2)
        edge = route['legs'][0]['path_mm'].pop()
        route['legs'][1]['path_mm'].insert(0, route['legs'][0]['path_mm'][-1])
        self.assertEqual(route['legs'][1]['path_mm'][1], edge)
        report = validate_route_evidence(self.value, self.layout, evaluation)
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('PATH_END_MISMATCH', {issue['code'] for issue in report['issues']})

    def test_measured_effective_length_uses_reachable_faces(self):
        evaluation = deepcopy(self.evaluation)
        evaluation['face_access'][0]['reachable'] = False
        evaluation['status'] = 'FAIL'
        measured = measure_operations(self.value, self.layout, evaluation)
        removed = next(module.length_mm for module in self.layout.shelves
                       if module.id == evaluation['face_access'][0]['module_id'])
        self.assertEqual(measured['effective_pick_length_mm'], measured['installed_face_length_mm'] - removed)
        self.assertFalse(measured['all_faces_reachable'])

    def test_fractional_depths_have_unique_reviewable_candidate_identities(self):
        value = self.value.model_copy(deep=True)
        value.base.rules.orientation_candidates = [0]
        for template in value.base.templates:
            template.depth_mm = 400.1 if template.depth_mm == 400 else 400.9
        # This uses the main rectangle and identical fixed demand; it neither
        # reads a holdout result nor changes the frozen fixture on disk.
        result = compare_operations(value)
        records = result['selection']['frontier']
        identifiers = [record['candidate_id'] for record in records]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertTrue(any('D400.1-' in identifier for identifier in identifiers))
        self.assertTrue(any('D400.9-' in identifier for identifier in identifiers))
        chosen = next(record for record in records
                      if record['candidate_id'] == result['selection']['chosen_candidate_id'])
        for key in ('depth_counts', 'effective_pick_length_mm', 'nominal_board_area_m2',
                    'picking_mean_mm', 'picking_worst_mm', 'replenishment_mean_mm', 'replenishment_worst_mm'):
            self.assertEqual(chosen['metrics'][key], result['after']['measurements'][key])

    def test_final_seal_rechecks_each_frozen_space_floor(self):
        for field, actual in [('minimum_pick_length_mm', self.layout.metrics['effective_pick_length_mm']),
                              ('minimum_nominal_board_area_m2', self.layout.metrics['nominal_board_area_m2'])]:
            with self.subTest(field=field):
                value = self.value.model_copy(deep=True)
                setattr(value.demand, field, actual + 1)
                with self.assertRaises(PlanningError) as context:
                    _seal(value, self.layout, self.evaluation)
                self.assertEqual(context.exception.code, 'OPERATIONS_DEMAND_FAILED')

    def test_unreachable_packing_point_keeps_every_task_and_prevents_seal(self):
        value, candidate = self.value.model_copy(deep=True), self.layout.model_copy(deep=True)
        module = candidate.shelves[0]
        value.workpoints.packing.point_mm = (module.x_mm, module.y_mm)
        candidate.workpoints = value.workpoints
        evaluation = evaluate_routes(value, candidate)
        self.assertEqual(evaluation['status'], 'FAIL')
        self.assertEqual(len(evaluation['picking_routes']), len(value.demand.picking_tasks))
        self.assertTrue(all(route['status'] == 'FAIL' for route in evaluation['picking_routes']))
        self.assertIsNone(evaluation['summary']['picking_mean_mm'])
        report = validate_route_evidence(value, candidate, evaluation)
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn({'code': 'WORKPOINT_NOT_CONNECTED_TO_ENTRANCE', 'id': 'packing'}, report['issues'])
        with self.assertRaises(PlanningError):
            _seal(value, candidate, evaluation)

    def test_passing_structure_only_layout_still_cannot_be_sealed(self):
        candidate = generate_candidate(self.value, 0, 400, (), 'BACK_TO_BACK', 'O0-D400-STRUCTURE_ONLY')
        evaluation = evaluate_routes(self.value, candidate)
        self.assertEqual(validate_operations_geometry(self.value, candidate)['status'], 'PASS')
        self.assertEqual(evaluation['status'], 'PASS')
        measured = measure_operations(self.value, candidate, evaluation)
        self.assertGreaterEqual(measured['effective_pick_length_mm'], self.value.demand.minimum_pick_length_mm)
        self.assertGreaterEqual(measured['nominal_board_area_m2'], self.value.demand.minimum_nominal_board_area_m2)
        self.assertFalse(candidate.planned_corridors)
        with self.assertRaises(PlanningError) as context:
            _seal(self.value, candidate, evaluation)
        self.assertNotEqual(context.exception.code, 'OPERATIONS_ROUTE_FAILED')
        self.assertNotEqual(context.exception.code, 'OPERATIONS_DEMAND_FAILED')

    def test_final_candidate_pool_requires_corridor_first_and_keeps_structure_comparison(self):
        captured = {}
        generate = operations_module.generate_candidate
        def capture(*args, **kwargs):
            candidate = generate(*args, **kwargs)
            captured[candidate.operational_policy] = candidate
            return candidate
        # A spy observes real main-scenario candidates; it does not replace
        # geometry, route evaluation, eligibility, or the selected result.
        with patch.object(operations_module, 'generate_candidate', side_effect=capture):
            result = compare_operations(self.value)
        records = result['selection']['frontier']
        structure = [record for record in records if record['stage'] == 'STRUCTURE_ONLY']
        self.assertTrue(structure)
        self.assertTrue(any(record.get('hard_constraints_pass') is True for record in structure))
        for record in structure:
            self.assertTrue(record.get('comparison_only'))
            self.assertFalse(record['eligible'])
        new_eligible = [record for record in records if record['eligible'] and record['candidate_id'] != 'M09']
        self.assertTrue(new_eligible)
        for record in new_eligible:
            self.assertEqual(record['stage'], 'CORRIDOR_FIRST')
            self.assertTrue(captured[record['candidate_id']].planned_corridors)
        selected_id = result['selection']['chosen_candidate_id']
        self.assertNotIn(selected_id, {record['candidate_id'] for record in structure})
        if selected_id != 'M09':
            self.assertTrue(result['after']['layout']['planned_corridors'])


if __name__ == '__main__':
    unittest.main()
