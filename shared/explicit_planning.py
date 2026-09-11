"""Explicit algorithm-validation geometry, separate from CAD readiness/signing."""
import hashlib
import json
from typing import Literal

from pydantic import Field
from shared.contracts import Model, PolygonData, Wall, Exclusion, Template, Business, Source
from shared.planning_v2 import PlanningRules, PlanningSpace, Confirmation, PlanRequest, space_geometry_digest


class ExplicitSpace(Model):
    units: Literal['mm'] = 'mm'
    boundary: PolygonData
    barriers: list[Wall] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    reserved_passages: list[Exclusion] = Field(default_factory=list)


class ExplicitPlanningInput(Model):
    schema_version: Literal['explicit-space-1.0'] = 'explicit-space-1.0'
    input_kind: Literal['ALGORITHM_VALIDATION']
    name: str = Field(min_length=1, max_length=100)
    space: ExplicitSpace
    templates: list[Template] = Field(min_length=1, max_length=64)
    rules: PlanningRules = Field(default_factory=PlanningRules)


def explicit_request(value: ExplicitPlanningInput) -> PlanRequest:
    value=ExplicitPlanningInput.model_validate(value.model_dump(mode='json'))
    raw=json.dumps(value.model_dump(mode='json'),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    source=Source(filename=f'ALGORITHM-{value.name}.json',sha256=hashlib.sha256(raw.encode()).hexdigest())
    space=PlanningSpace(boundary=value.space.boundary,barriers=value.space.barriers,
        exclusions=value.space.exclusions,entrances=value.space.reserved_passages,
        source_evidence=[{'type':'ALGORITHM_VALIDATION','name':value.name,
                          'geometry_basis':'EXPLICIT_PARAMETERS','real_store':False,'cad_processed':False}],
        confirmation=Confirmation(state='SYNTHETIC_TEST',confirmed_by='explicit algorithm input',
            source_sha256=source.sha256,note='Explicit validation space; no human CAD confirmation or signature.'))
    space.confirmation.geometry_sha256=space_geometry_digest(space)
    return PlanRequest(source=source,space=space,rules=value.rules,
        business=Business(products=[],product_count=0,templates=value.templates,
                          sources=[{'kind':'EXPLICIT_ALGORITHM_TEMPLATES'}]))
