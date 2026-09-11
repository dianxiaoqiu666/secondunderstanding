"""Independent parameterized spaces. No real CAD coordinates or reference input."""
import hashlib
from shared.contracts import PolygonData, Wall, Exclusion, Source
from shared.planning_v2 import PlanningSpace, Confirmation, PlanRequest, space_geometry_digest

def ring(x,y,w,h):
    return [(x,y),(x+w,y),(x+w,y+h),(x,y+h),(x,y)]

def rectangle(name,width,height,offset=(0,0),obstacle=None):
    x,y=offset
    space=PlanningSpace(boundary=PolygonData(boundary_mm=ring(x,y,width,height)),
        confirmation=Confirmation(state='SYNTHETIC_TEST',confirmed_by='synthetic fixture'),
        source_evidence=[{'type':'SYNTHETIC','name':name,'dimensions_mm':[width,height]}])
    cx,cy=x+width/2,y+height/2
    if obstacle=='column':
        space.exclusions=[Exclusion(id='column',boundary_mm=ring(cx-700,cy-700,1400,1400))]
    elif obstacle=='wall':
        space.barriers=[Wall(id='wall',start_mm=(cx,y+height*.3),end_mm=(cx,y+height*.7))]
    elif obstacle=='hole':
        space.boundary.holes_mm=[ring(cx-1000,cy-1000,2000,2000)]
    elif obstacle=='entrance':
        space.entrances=[Exclusion(id='entry-clearance',boundary_mm=ring(cx-1500,y,3000,2500))]
    elif obstacle=='concave':
        space.boundary.boundary_mm=[(x,y),(x+width,y),(x+width,y+height*.5),
            (x+width*.6,y+height*.5),(x+width*.6,y+height),(x,y+height),(x,y)]
    space.confirmation.geometry_sha256=space_geometry_digest(space)
    return space

def cases():
    items=[('square',12000,12000,(0,0)),('long',6000,24000,(0,0)),
           ('wide',24000,6000,(0,0)),('medium',18000,12000,(0,0)),
           ('large',30000,20000,(0,0)),('translated',18000,12000,(135000,-97000))]
    result={name:rectangle(name,w,h,xy) for name,w,h,xy in items}
    result.update({kind:rectangle(kind,18000,12000,obstacle=kind)
                   for kind in ['column','wall','hole','entrance','concave']})
    return result

def request(name,space,business):
    source=Source(filename=f'SYNTHETIC-{name}',sha256=hashlib.sha256(name.encode()).hexdigest())
    space=space.model_copy(deep=True)
    space.confirmation.source_sha256=source.sha256
    space.confirmation.geometry_sha256=space_geometry_digest(space)
    return PlanRequest(source=source,space=space,business=business)
