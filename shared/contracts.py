"""Versioned, JSON-only boundaries for the three independently runnable services."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

Point = tuple[FiniteFloat, FiniteFloat]

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')

class PolygonData(Model):
    boundary_mm: list[Point] = Field(min_length=4)
    holes_mm: list[list[Point]] = Field(default_factory=list)

class Exclusion(PolygonData):
    id: str

class Wall(Model):
    id: str
    start_mm: Point
    end_mm: Point

class Spatial(Model):
    units: Literal['mm'] = 'mm'
    scope: PolygonData
    walls: list[Wall]
    exclusion_zones: list[Exclusion]
    provenance: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

class Source(Model):
    filename: str
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

class Template(Model):
    material_id: str
    material_type: Literal['SHELF'] = 'SHELF'
    length_mm: FiniteFloat = Field(gt=0)
    depth_mm: FiniteFloat = Field(gt=0)
    default_level_count: int = Field(gt=0,le=30)

class Business(Model):
    products: list[dict[str, Any]]
    product_count: int = Field(ge=0)
    templates: list[Template] = Field(min_length=1)
    sources: list[dict[str, Any]]

class UnderstandingPackage(Model):
    schema_version: Literal['1.0'] = '1.0'
    source: Source
    spatial: Spatial
    business: Business

class Shelf(Model):
    id: str
    material_id: str
    x_mm: FiniteFloat
    y_mm: FiniteFloat
    rotation_deg: Literal[0,90]
    length_mm: FiniteFloat = Field(gt=0)
    depth_mm: FiniteFloat = Field(gt=0)
    default_level_count: int = Field(gt=0)
    footprint_mm: list[Point] = Field(min_length=4)

class BOMRow(Model):
    material_id: str
    length_mm: FiniteFloat = Field(gt=0)
    depth_mm: FiniteFloat = Field(gt=0)
    default_level_count: int = Field(gt=0)
    quantity: int = Field(gt=0)

class LayoutResult(Model):
    schema_version: Literal['1.0'] = '1.0'
    source: Source
    spatial: Spatial
    rules: dict[str, Any]
    shelves: list[Shelf] = Field(min_length=1)
    bom: list[BOMRow] = Field(min_length=1)
    validation: dict[str, Any]
    planning: dict[str, Any]
    business_summary: dict[str, Any]
    warnings: list[str]
