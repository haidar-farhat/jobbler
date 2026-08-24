"""Export the agent protocol as JSON Schema.

    python scripts/export_schemas.py

`contracts.py` is the single source of truth. Rather than hand-maintaining JSON Schema
alongside Pydantic models and letting them drift, the schemas are generated from the models.
Writing them to `packages/shared-types/schemas/` keeps the contract reviewable in a diff and
gives the TypeScript side something to generate from:

    npx json-schema-to-typescript -i packages/shared-types/schemas -o apps/web/src/generated
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from localapply.contracts import (  # noqa: E402
    ActionResult,
    AgentEvent,
    Decision,
    Observation,
    ObservedElement,
    PolicyVerdict,
)

MODELS = [Observation, ObservedElement, Decision, PolicyVerdict, ActionResult, AgentEvent]
OUTPUT_DIR = Path(__file__).resolve().parents[3] / "packages" / "shared-types" / "schemas"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        path = OUTPUT_DIR / f"{model.__name__}.json"
        path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(OUTPUT_DIR.parents[2])}")


if __name__ == "__main__":
    main()
