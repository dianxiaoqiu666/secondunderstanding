"""Purpose-specific upload reading; reference classification is retained."""
import hashlib
from pathlib import Path
from fastapi import APIRouter, File, HTTPException, UploadFile

router=APIRouter()


@router.post('/existing-design')
def existing_design(file:UploadFile=File(...)):
    from services.understanding.prepare import MAX_CAD_BYTES, ROOT
    from services.understanding.existing_design import extract_existing_design
    from shared.existing_design import validate_existing
    try:
        data=file.file.read(MAX_CAD_BYTES+1)
        filename=Path((file.filename or '').replace('\\','/')).name
        if not filename.lower().endswith('.dxf') or not data or len(data)>MAX_CAD_BYTES:
            raise ValueError('请上传非空且不超过 25 MB 的 DXF。')
        result=extract_existing_design(data,filename)
        sha=hashlib.sha256(data).hexdigest()
        classification='USER_UPLOADED_FOR_VIEW'
        for line in (ROOT/'DATA_CLASSIFICATION.md').read_text(encoding='utf-8-sig').splitlines():
            if sha in line and line.startswith('|'):
                fields=[part.strip() for part in line.split('|')]
                if len(fields)>5: classification=fields[5]
        result['source_classification']=classification
        if result['source']['sha256']!=sha:
            raise ValueError('已有设计提取来源摘要不一致。')
        return validate_existing(result)
    except (ValueError,KeyError,TypeError) as exc:
        raise HTTPException(422,detail={'code':'EXISTING_DESIGN_UNREADABLE','message':str(exc)}) from exc
