"""General layout regression fixtures; the new independent holdout is not read."""
import json
from pathlib import Path
import unittest

from shapely.geometry import LineString, Point, Polygon

from shared.operations_planning import OperationsInput
from services.planning.engine import PlanningError
from services.planning.operations import _eligibility, _seal, measure_operations
from services.planning.operations_layouts import checked_input, generate_candidate, residual_single_options, validate_operations_geometry
from services.planning.operations_routes import corridor_plans, evaluate_routes, validate_route_evidence

ROOT=Path(__file__).resolve().parents[2]


def fixture(name, catalog='scenarios_m10.json'):
    data=json.loads((ROOT/'tools/benchmarks'/catalog).read_text(encoding='utf-8-sig'))
    return OperationsInput.model_validate(next(row['input'] for row in data['scenarios'] if row['id']==name))


class FrontageIntervals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.value=fixture('op-independent-holdout','scenarios_m10_holdout.json')
        cls.plan=next(p for p in corridor_plans(cls.value,0,400) if p['id']=='PERIMETER_LOOP_WITH_ENTRY_CROSS_LINK')
        cls.old=generate_candidate(cls.value,0,400,cls.plan['corridors'])
        cls.fixed=generate_candidate(cls.value,0,400,cls.plan['corridors'],frontage_aware=True)
        cls.evaluation=evaluate_routes(cls.value,cls.fixed)

    def test_obstacle_outside_rack_strip_cannot_consume_pick_standing_space(self):
        obstacle=Polygon(self.value.base.space.exclusions[0].boundary_mm)
        self.assertTrue(any(obstacle.covers(Point(face.standing_point_mm)) for face in self.old.pick_faces))
        self.assertTrue(all(obstacle.distance(Point(face.standing_point_mm))+1e-6>=600
                            for face in self.fixed.pick_faces))
        self.assertEqual(self.evaluation['status'],'PASS')
        self.assertEqual(self.evaluation['summary']['reachable_face_count'],len(self.fixed.shelves))
        self.assertEqual(self.fixed.validation['geometry_violation_count'],0)

    def test_recovered_candidate_preserves_fixed_tasks_rules_and_both_floors(self):
        metrics=measure_operations(self.value,self.fixed,self.evaluation)
        self.assertFalse(_eligibility(self.value,metrics,self.evaluation))
        self.assertEqual(self.fixed.rules.aisle_width_mm,1200)
        self.assertEqual(metrics['effective_pick_length_mm'],175200)
        self.assertEqual(metrics['nominal_board_area_m2'],444)
        for kind in ('picking','replenishment'):
            expected={task.id:set(task.item_ids) for task in getattr(self.value.demand,f'{kind}_tasks')}
            routes=self.evaluation[f'{kind}_routes']
            self.assertEqual({route['id'] for route in routes},set(expected))
            for route in routes:self.assertEqual(set(route['visited_item_ids']),expected[route['id']])
        proof=validate_route_evidence(self.value,self.fixed,self.evaluation)
        self.assertEqual(proof['status'],'PASS',proof['issues'])
        self.assertFalse(proof['full_operations_proven'],'Legacy inside point is not an outside-door proof')

    def test_forged_face_and_interval_metadata_are_independently_rejected(self):
        bad=self.fixed.model_copy(deep=True)
        bad.pick_faces[0].standing_point_mm=(12400,8000)
        with self.assertRaises(PlanningError):validate_operations_geometry(self.value,bad)
        bad=self.fixed.model_copy(deep=True)
        bad.runs[0].available_length_mm+=1200
        bad.runs[0].remaining_length_mm+=1200
        with self.assertRaises(PlanningError):validate_operations_geometry(self.value,bad)

    def test_already_clear_short_entry_keeps_exact_modules_faces_and_bom(self):
        value=fixture('op-short')
        plan=next(p for p in corridor_plans(value,0,400) if p['id']=='PERIMETER_LOOP_WITH_ENTRY_CROSS_LINK')
        old=generate_candidate(value,0,400,plan['corridors'])
        fixed=generate_candidate(value,0,400,plan['corridors'],frontage_aware=True)
        self.assertEqual((old.shelves,old.pick_faces,old.bom),(fixed.shelves,fixed.pick_faces,fixed.bom))
        self.assertEqual(fixed.metrics['same_run_module_max_gap_mm'],0)
        self.assertEqual(fixed.metrics['parallel_pick_aisle_min_mm'],1200)
        self.assertEqual(fixed.metrics['parallel_pick_aisle_max_mm'],2100)

    def test_width_sections_are_opposing_clear_front_spans_not_diagonal_distances(self):
        shapes={assembly.id:Polygon(assembly.footprint_mm) for assembly in self.fixed.assemblies}
        for section in self.fixed.metrics['aisle_sections']:
            segment=LineString([section['start_mm'],section['end_mm']])
            self.assertAlmostEqual(segment.length,section['clear_width_mm'])
            self.assertGreaterEqual(segment.length,1200)
            self.assertGreater(section['overlap_length_mm'],0)
            for identifier in section['between_assemblies']:
                self.assertAlmostEqual(segment.distance(shapes[identifier]),0)
            self.assertTrue(all(segment.intersection(shape.buffer(-1e-6)).is_empty for shape in shapes.values()))
        self.assertEqual(self.fixed.metrics['internal_back_to_back_gap_mm'],[0])

    def test_real_wall_evidence_and_full_door_proof_cannot_be_replaced_by_scope_line(self):
        value=fixture('op-short','scenarios_m11.json')
        checked_input(value)
        bad=value.model_copy(deep=True)
        bad.physical_perimeter_walls=[]
        with self.assertRaises(PlanningError):checked_input(bad)
        evaluation=dict(self.evaluation,full_operations_proven=False)
        measured=measure_operations(value,self.fixed,evaluation)
        reasons=_eligibility(value,measured,evaluation)
        self.assertTrue(any('店外' in reason for reason in reasons))


class MixedWallDirections(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.value=fixture('op-short','scenarios_m11.json')
        cls.plan=next(p for p in corridor_plans(cls.value,0,400) if p['id']=='WALL_FRONT_LOOP_WITH_ENTRY_CROSS_LINK')
        cls.layout=generate_candidate(cls.value,0,400,cls.plan['corridors'],frontage_aware=True,all_wall_directions=True)
        cls.evaluation=evaluate_routes(cls.value,cls.layout)

    def test_all_wall_directions_remain_sourced_with_clearance_and_complete_faces(self):
        layout=self.layout
        wall_runs=[run for run in layout.runs if any(a.kind=='WALL_SINGLE' and run.run_id in a.run_ids for a in layout.assemblies)]
        self.assertEqual({run.direction_deg for run in wall_runs},{0,90})
        self.assertEqual(layout.metrics['wall_single_run_count'],5)
        self.assertEqual(layout.metrics['same_run_module_max_gap_mm'],0)
        self.assertGreater(layout.metrics['double_sided_run_group_count'],0)
        self.assertEqual(self.evaluation['status'],'PASS')
        self.assertTrue(self.evaluation['full_operations_proven'])
        self.assertEqual(self.evaluation['summary']['reachable_face_count'],len(layout.shelves))
        self.assertTrue(all(row['back_setback_mm']==100 for row in layout.metrics['wall_run_sections']))
        self.assertTrue(all(row['back_space_role']=='REQUIRED_WALL_CLEARANCE_NO_PICK_AISLE' for row in layout.metrics['wall_run_sections']))
        self.assertEqual(validate_route_evidence(self.value,layout,self.evaluation)['status'],'PASS')

    def test_explicit_main_axis_is_validated_when_first_wall_is_perpendicular(self):
        value=self.value.model_copy(deep=True)
        value.usable_walls=[wall for wall in value.usable_walls if wall.start_mm[0]==wall.end_mm[0]]
        plan=next(p for p in corridor_plans(value,0,400) if p['id']=='WALL_FRONT_LOOP')
        layout=generate_candidate(value,0,400,plan['corridors'],frontage_aware=True,all_wall_directions=True)
        self.assertEqual(layout.runs[0].direction_deg,90)
        self.assertEqual(layout.metrics['generation_spec']['main_direction_deg'],0)
        self.assertEqual(_seal(value,layout,evaluate_routes(value,layout)).validation['status'],'PASS')
        bad=layout.model_copy(deep=True)
        bad.metrics['generation_spec']['main_direction_deg']=90
        with self.assertRaises(PlanningError):validate_operations_geometry(value,bad)

    def test_greater_wall_clearance_is_preserved_in_layout_and_front_loop(self):
        value=self.value.model_copy(deep=True)
        value.base.rules.wall_clearance_mm=200
        plan=next(p for p in corridor_plans(value,0,400) if p['id']=='WALL_FRONT_LOOP')
        layout=generate_candidate(value,0,400,plan['corridors'],frontage_aware=True,all_wall_directions=True)
        self.assertEqual(layout.rules.aisle_width_mm,1200)
        self.assertTrue(all(row['back_setback_mm']==200 for row in layout.metrics['wall_run_sections']))
        self.assertEqual(layout.validation['geometry_violation_count'],0)


class ResidualCentralSingle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.value=fixture('op-short','scenarios_m11.json')
        cls.plan=next(p for p in corridor_plans(cls.value,0,400) if p['id']=='WALL_FRONT_LOOP_WITH_ENTRY_CROSS_LINK')
        cls.layout=generate_candidate(cls.value,0,400,cls.plan['corridors'],frontage_aware=True,
            all_wall_directions=True,residual_single_at='LOW')

    def test_geometric_residue_really_fits_one_single_but_not_another_complete_pair(self):
        layout=self.layout;space=layout.metrics['residual_space_use']
        self.assertEqual(space['original_residual_width_mm'],1800)
        self.assertEqual(space['single_plus_aisle_required_mm'],1600)
        self.assertEqual(space['additional_double_plus_aisle_required_mm'],2000)
        self.assertEqual(space['residual_after_single_mm'],200)
        self.assertEqual(layout.metrics['effective_pick_length_mm'],164400)
        self.assertEqual(layout.metrics['nominal_board_area_m2'],449.28)
        self.assertEqual(layout.metrics['parallel_pick_aisle_max_mm'],1300)
        self.assertEqual(layout.metrics['same_run_module_max_gap_mm'],0)
        evaluation=evaluate_routes(self.value,layout)
        self.assertTrue(evaluation['full_operations_proven'])
        self.assertEqual(validate_route_evidence(self.value,layout,evaluation)['status'],'PASS')

    def test_extra_single_back_aisle_has_full_opposing_pick_service(self):
        services=self.layout.metrics['residual_single_back_service']
        self.assertTrue(services)
        for row in services:
            self.assertTrue(row['full_back_aisle_has_pick_service'])
            self.assertEqual(row['served_back_length_mm'],row['required_back_length_mm'])
            self.assertTrue(all(witness['served_face_ids'] and witness['clear_width_mm']>=1200
                                for witness in row['witnesses']))
        value=self.value.model_copy(deep=True)
        value.usable_walls=[wall for wall in value.usable_walls if wall.inward_normal!=(0,1)]
        with self.assertRaisesRegex(PlanningError,'背侧过道'):
            generate_candidate(value,0,400,self.plan['corridors'],frontage_aware=True,
                all_wall_directions=True,residual_single_at='LOW')

    def test_no_residual_branch_without_room_and_no_forged_width_explanation(self):
        narrow=fixture('op-holdout','scenarios_m11.json')
        self.assertEqual(residual_single_options(narrow,90,600),())
        bad=self.layout.model_copy(deep=True)
        bad.metrics['residual_space_use']['original_residual_width_mm']=10000
        with self.assertRaises(PlanningError):validate_operations_geometry(self.value,bad)


if __name__=='__main__':unittest.main()
