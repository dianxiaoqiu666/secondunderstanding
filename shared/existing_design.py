"""Observed CAD contract: validate what was read without generating a design."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math

from shapely.geometry import Polygon
from shared.contracts import Source, PolygonData, Wall, Exclusion


def canonical_digest(value):
    copied=deepcopy(value)
    copied.pop('deterministic_sha256',None)
    return hashlib.sha256(json.dumps(copied,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def validate_existing(value):
    result=deepcopy(value)
    if result.get('schema_version')!='existing-1.0' or result.get('task_purpose')!='existing_design' or result.get('units')!='mm':
        raise ValueError('已有设计必须使用 existing-1.0 毫米观察数据契约。')
    Source.model_validate(result['source'])
    space=result['space']
    if space.get('boundary') is not None:
        boundary=PolygonData.model_validate(space['boundary'])
        polygon=Polygon(boundary.boundary_mm,boundary.holes_mm)
        if polygon.is_empty or not polygon.is_valid or polygon.area<=0:
            raise ValueError('已有设计范围无效；应省略并说明，不能构造虚假地面。')
    for wall in space['barriers']: Wall.model_validate(wall)
    for zone in [*space['exclusions'],*space['entrances']]:
        parsed=Exclusion.model_validate(zone)
        if not Polygon(parsed.boundary_mm,parsed.holes_mm).is_valid:
            raise ValueError('已有设计区域几何无效。')
    ids=set();counts=Counter()
    for item in result['shelves']:
        if item.get('classification')!='ACTUAL' or item['id'] in ids:
            raise ValueError('只有身份明确且唯一的实际货架能进入观察清单。')
        ids.add(item['id'])
        if not item.get('source_handles'):
            raise ValueError('货架缺少上传图元来源。')
        for key in ('x_mm','y_mm','length_mm','depth_mm','rotation_deg'):
            if not isinstance(item.get(key),(int,float)) or not math.isfinite(item[key]):
                raise ValueError('货架实测位置或规格无效。')
        polygon=Polygon(item['footprint_mm'])
        if not polygon.is_valid or polygon.area<=0 or item['length_mm']<=0 or item['depth_mm']<=0:
            raise ValueError('货架观察轮廓无效。')
        level=item.get('default_level_count')
        if level is not None and (type(level) is not int or not 1<=level<=30):
            raise ValueError('默认层数必须未知或来自物料配置的有效整数。')
        dimensions=item.get('nominal_specification_mm',[round(item['length_mm'],3),round(item['depth_mm'],3)])
        counts[(item.get('material_id'),*dimensions,level)]+=1
    observed=Counter()
    for row in result['bom']:
        if type(row['quantity']) is not int or row['quantity']<=0:
            raise ValueError('已有货架数量无效。')
        observed[(row.get('material_id'),row['length_mm'],row['depth_mm'],row.get('default_level_count'))]+=row['quantity']
    if counts!=observed:
        raise ValueError('已有货架实例和观察清单不一致。')
    for item in [*result['legend_samples'],*result['unknown_objects']]:
        if item.get('id') in ids:
            raise ValueError('图例或未知对象不能同时计为实际货架。')
    result['validation']={'status':'OBSERVATION_CHECKED','new_layout_generated':False,
                          'shelf_bom_consistent':True,'planning_safety_assessed':False}
    result['deterministic_sha256']=canonical_digest(result)
    return result


def verify_existing_digest(value):
    if value.get('deterministic_sha256')!=canonical_digest(value):
        raise ValueError('保存的已有设计摘要不一致。')
    validate_existing(value)
