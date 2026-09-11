"""Frozen algorithm-first boundaries. ReferenceDesignProfile is never a PlanRequest field."""
from typing import Any, Literal
from pydantic import Field, FiniteFloat, model_validator
from shared.contracts import Model, PolygonData, Exclusion, Wall, Source, Business, Shelf, BOMRow, Point
import hashlib
import json

class Confirmation(Model):
    state: Literal['REQUIRES_CONFIRMATION','AUTO_VALIDATED','CONFIRMED','SYNTHETIC_TEST'] = 'REQUIRES_CONFIRMATION'
    geometry_sha256: str = ''
    confirmed_by: str = ''
    note: str = ''
    source_sha256: str = ''
    reviewed_fields: list[str] = Field(default_factory=list)
    token: str = ''

class PlanningSpace(Model):
    units: Literal['mm'] = 'mm'
    boundary: PolygonData
    barriers: list[Wall] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    entrances: list[Exclusion] = Field(default_factory=list)
    source_evidence: list[dict[str, Any]] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    confirmation: Confirmation = Field(default_factory=Confirmation)

class PlanningRules(Model):
    aisle_width_mm: FiniteFloat = Field(default=1200,gt=0)
    boundary_clearance_mm: FiniteFloat = Field(default=100,ge=0)
    wall_clearance_mm: FiniteFloat = Field(default=100,ge=0)
    end_aisle_mm: FiniteFloat = Field(default=1200,gt=0)
    min_modules_per_run: int = Field(default=2,ge=2)
    orientation_candidates: list[Literal[0,90]] = Field(default_factory=lambda:[0,90])

class ShelfRun(Model):
    run_id: str
    direction_deg: Literal[0,90]
    start_mm: Point
    end_mm: Point
    available_length_mm: FiniteFloat = Field(gt=0)
    depth_mm: FiniteFloat = Field(gt=0)
    modules: list[Shelf] = Field(min_length=2)
    used_length_mm: FiniteFloat = Field(gt=0)
    remaining_length_mm: FiniteFloat = Field(ge=0)
    footprint_mm: list[Point] = Field(min_length=4)
    neighbors: list[dict[str,Any]] = Field(default_factory=list)

class ProductPlacementPlan(Model):
    category_basis: str = 'source category field; no inferred physical dimensions'
    category_assignments: list[dict[str,Any]] = Field(default_factory=list)
    unassigned_categories: list[dict[str,Any]] = Field(default_factory=list)
    real_sku_status: Literal['UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA'] = 'UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA'
    sku_assignments: list[dict[str,Any]] = Field(default_factory=list)
    unassigned_products: list[dict[str,Any]] = Field(default_factory=list)

    @model_validator(mode='after')
    def no_fabricated_real_sku(self):
        if self.sku_assignments:
            raise ValueError('Real SKU dimensions are unavailable; simulated assignments belong in a separate simulation result')
        return self

class SyntheticProduct(Model):
    product_id: str
    category: str
    width_mm: FiniteFloat = Field(gt=0)
    depth_mm: FiniteFloat = Field(gt=0)
    height_mm: FiniteFloat = Field(gt=0)
    demand_weight: FiniteFloat = Field(default=1,ge=0)
    minimum_facings: int = Field(default=1,gt=0)
    rotation_policy: Literal['FIXED','ALLOW_XY_SWAP'] = 'FIXED'

class ShelfLevel(Model):
    shelf_id: str
    level_index: int = Field(ge=0)
    width_mm: FiniteFloat = Field(gt=0)
    depth_mm: FiniteFloat = Field(gt=0)
    clear_height_mm: FiniteFloat = Field(gt=0)
    category: str | None = None

class PlanRequest(Model):
    schema_version: Literal['2.0'] = '2.0'
    source: Source
    space: PlanningSpace
    business: Business
    rules: PlanningRules = Field(default_factory=PlanningRules)

class BOMRowV2(BOMRow):
    total_level_count: int = Field(gt=0)

class LayoutV2(Model):
    schema_version: Literal['2.0'] = '2.0'
    source: Source
    space: PlanningSpace
    rules: PlanningRules
    runs: list[ShelfRun] = Field(min_length=1)
    shelves: list[Shelf] = Field(min_length=2)
    bom: list[BOMRowV2] = Field(min_length=1)
    products: ProductPlacementPlan = Field(default_factory=ProductPlacementPlan)
    metrics: dict[str,Any]
    validation: dict[str,Any]
    design_status: Literal['2D_REVIEW_REQUIRED','ACCEPTED','VALIDATED'] = '2D_REVIEW_REQUIRED'

class ReferenceDesignProfile(Model):
    schema_version: Literal['reference-1.0'] = 'reference-1.0'
    source: Source
    classification: list[dict[str,Any]]
    runs: list[dict[str,Any]]
    metrics: dict[str,Any]
    functional_relations: list[dict[str,Any]]
    bom: list[dict[str,Any]]
    method: dict[str,Any]
    unresolved: list[str]

def space_geometry_digest(space: PlanningSpace) -> str:
    value=space.model_dump(mode='json',include={'units','boundary','barriers','exclusions','entrances'})
    value['source_sha256']=space.confirmation.source_sha256
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
