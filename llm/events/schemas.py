"""Generate versioned schemas; --check is read-only."""

import argparse
import json
from pathlib import Path

from .models import ArticleInput, ArticleRecord, EventBundle, GoldSet

ROOT = Path(__file__).resolve().parents[2]
MODELS = {"event-bundle": EventBundle, "article-input": ArticleInput,
          "article-record": ArticleRecord, "event-gold": GoldSet}


def generated_schemas():
    for name, model in MODELS.items():
        schema = model.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        yield ROOT / f"docs/contracts/{name}.v0.2.schema.json", schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed schemas differ from models")
    args = parser.parse_args()
    for path, schema in generated_schemas():
        if args.check:
            if not path.exists() or json.loads(path.read_text()) != schema:
                parser.exit(1, f"Schema drift: {path}\n")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
    print("Schemas match models." if args.check else "Generated four v0.2 schemas.")


if __name__ == "__main__":
    main()
