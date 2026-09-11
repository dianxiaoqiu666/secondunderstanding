"""Project-local signing of validated source facts and necessary human supplements."""
import hashlib
import hmac
import json
from pathlib import Path
import secrets
from shared.planning_v2 import PlanRequest,space_geometry_digest

ROOT=Path(__file__).resolve().parents[1]
KEY_PATH=ROOT/'runtime/confirmation.key'
REVIEW_FIELDS={'boundary','holes','barriers','exclusions','entrances'}

def _key(create=False):
    if create and not KEY_PATH.exists():
        KEY_PATH.parent.mkdir(parents=True,exist_ok=True)
        try:
            with KEY_PATH.open('xb') as stream:stream.write(secrets.token_bytes(32))
        except FileExistsError:pass
    if not KEY_PATH.is_file():raise ValueError('CONFIRMATION_KEY_MISSING')
    value=KEY_PATH.read_bytes()
    if len(value)!=32:raise ValueError('CONFIRMATION_KEY_INVALID')
    return value

def _payload(req):
    return json.dumps({'source':req.source.model_dump(mode='json'),
        'geometry':space_geometry_digest(req.space),'business':req.business.model_dump(mode='json'),
        'state':req.space.confirmation.state,
        'source_evidence':req.space.source_evidence,
        'note':req.space.confirmation.note,
        'confirmed_by':req.space.confirmation.confirmed_by,
        'rules':req.rules.model_dump(mode='json'),
        'reviewed_fields':sorted(req.space.confirmation.reviewed_fields)},
        sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')

def sign_request(req:PlanRequest):
    if req.space.confirmation.state not in {'CONFIRMED','AUTO_VALIDATED'}:raise ValueError('VALIDATED_SPACE_REQUIRED')
    if req.space.unresolved:raise ValueError('UNRESOLVED_SPATIAL_EVIDENCE')
    if req.space.confirmation.state=='CONFIRMED' and not req.space.confirmation.reviewed_fields:raise ValueError('SPATIAL_REVIEW_INCOMPLETE')
    if set(req.space.confirmation.reviewed_fields)-REVIEW_FIELDS:raise ValueError('SPATIAL_REVIEW_INVALID')
    if not req.space.confirmation.confirmed_by.strip():raise ValueError('CONFIRMATION_ACTOR_MISSING')
    req.space.confirmation.source_sha256=req.source.sha256
    req.space.confirmation.geometry_sha256=space_geometry_digest(req.space)
    req.space.confirmation.token=hmac.new(_key(create=True),_payload(req),hashlib.sha256).hexdigest()
    return req

def verify_request(req:PlanRequest):
    confirmation=req.space.confirmation
    if confirmation.state not in {'CONFIRMED','AUTO_VALIDATED'}:raise ValueError('VALIDATED_SPACE_REQUIRED')
    if not confirmation.confirmed_by.strip():raise ValueError('CONFIRMATION_ACTOR_MISSING')
    if req.space.unresolved:raise ValueError('UNRESOLVED_SPATIAL_EVIDENCE')
    if confirmation.state=='CONFIRMED' and not confirmation.reviewed_fields:raise ValueError('SPATIAL_REVIEW_INCOMPLETE')
    if set(confirmation.reviewed_fields)-REVIEW_FIELDS:raise ValueError('SPATIAL_REVIEW_INVALID')
    if confirmation.source_sha256!=req.source.sha256:raise ValueError('CONFIRMATION_SOURCE_MISMATCH')
    if confirmation.geometry_sha256!=space_geometry_digest(req.space):raise ValueError('CONFIRMED_GEOMETRY_CHANGED')
    expected=hmac.new(_key(),_payload(req),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,confirmation.token):raise ValueError('CONFIRMATION_SIGNATURE_INVALID')
