"""Dump the current backend OpenAPI schema to a file (PR #4 contract
freshness): python dump_openapi.py <output-path>."""

import json
import sys

from app.main import create_app

target = sys.argv[1]
schema = create_app().openapi()
with open(target, "w", encoding="utf-8") as handle:
    json.dump(schema, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
print(f"wrote {target}")
