"""Export the versioned public contracts without reading production sources."""
import json
from pathlib import Path
from shared.contracts import UnderstandingPackage, LayoutResult
from shared.planning_v2 import PlanRequest, LayoutV2, ProductPlacementPlan, ReferenceDesignProfile


def main():
    target = Path(__file__).resolve().parents[1] / 'shared/schemas'
    target.mkdir(parents=True, exist_ok=True)
    for model in (UnderstandingPackage, LayoutResult, PlanRequest, LayoutV2,
                  ProductPlacementPlan, ReferenceDesignProfile):
        text = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + '\n'
        path = target / f'{model.__name__}.schema.json'
        if not path.exists() or path.read_text(encoding='utf-8-sig') != text:
            path.write_text(text, encoding='utf-8')


if __name__ == '__main__':
    main()
