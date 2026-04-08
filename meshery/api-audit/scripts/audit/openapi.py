"""OpenAPI spec parser and schema field extraction."""

import sys

from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pip install pyyaml")

from .config import HTTP_METHODS
from .models import normalize_path



def _build_tag_category_map(doc: dict) -> Dict[str, Tuple[str, str]]:
    """Build a mapping from OpenAPI tag name to (category, subcategory)."""
    tag_display: Dict[str, str] = {}
    for tag_def in doc.get("tags", []):
        if isinstance(tag_def, dict) and "name" in tag_def:
            display = tag_def.get("x-displayName", tag_def["name"])
            tag_display[tag_def["name"]] = display

    tag_to_category: Dict[str, Tuple[str, str]] = {}
    for group in doc.get("x-tagGroups", []):
        if not isinstance(group, dict):
            continue
        group_name = group.get("name", "Other")
        for tag_name in group.get("tags", []):
            display = tag_display.get(tag_name, tag_name)
            tag_to_category[tag_name] = (group_name, display)

    for tag_name, display in tag_display.items():
        if tag_name not in tag_to_category:
            tag_to_category[tag_name] = (display, display)

    return tag_to_category


def parse_openapi(spec_file: Path) -> dict:
    """Parse the authoritative bundled OpenAPI spec.

    Returns a dict with:
      all_paths:        {norm_path: {METHOD, ...}}
      x_internal:       {(norm_path, METHOD): ["cloud"] or []}
      original_paths:   {norm_path: original_path}
      path_categories:  {norm_path: (category, subcategory)}
      operations:       {(norm_path, METHOD): operation_dict}
    """
    empty = {
        "all_paths": {},
        "x_internal": {},
        "original_paths": {},
        "path_categories": {},
        "operations": {},
    }
    if not spec_file.exists():
        print(f"ERROR: {spec_file} not found", file=sys.stderr)
        return empty

    with open(spec_file, encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    tag_to_category = _build_tag_category_map(doc)

    all_paths: Dict[str, Set[str]] = {}
    x_internal: Dict[Tuple[str, str], List[str]] = {}
    original_paths: Dict[str, str] = {}
    path_categories: Dict[str, Tuple[str, str]] = {}
    operations: Dict[Tuple[str, str], dict] = {}

    for path, methods_obj in doc.get("paths", {}).items():
        if not isinstance(methods_obj, dict):
            continue
        for method, details in methods_obj.items():
            if method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(details, dict):
                continue

            norm = normalize_path(path)
            m_upper = method.upper()
            all_paths.setdefault(norm, set()).add(m_upper)

            if norm not in original_paths:
                original_paths[norm] = path

            if norm not in path_categories:
                op_tags = details.get("tags", [])
                if isinstance(op_tags, list):
                    for tag_name in op_tags:
                        if tag_name in tag_to_category:
                            path_categories[norm] = tag_to_category[tag_name]
                            break

            xi = details.get("x-internal", [])
            if not isinstance(xi, list):
                xi = [xi] if xi else []
            x_internal[(norm, m_upper)] = xi

            operations[(norm, m_upper)] = details

    return {
        "all_paths": all_paths,
        "x_internal": x_internal,
        "original_paths": original_paths,
        "path_categories": path_categories,
        "operations": operations,
    }


# ---------------------------------------------------------------------------
# Spec schema field extraction (for cross-check completeness)
# ---------------------------------------------------------------------------

def collect_property_names(schema: Any) -> Set[str]:
    """Recursively collect property names from an OpenAPI schema."""
    if not isinstance(schema, dict):
        return set()

    props = set()

    if "properties" in schema and isinstance(schema["properties"], dict):
        props.update(schema["properties"].keys())

    for combo in ("allOf", "oneOf", "anyOf"):
        if combo in schema and isinstance(schema[combo], list):
            for sub in schema[combo]:
                props.update(collect_property_names(sub))

    if schema.get("type") == "array" and isinstance(
        schema.get("items"), dict
    ):
        props.update(collect_property_names(schema["items"]))

    return props


def extract_spec_schema_fields(
    operation: dict, method: str
) -> Dict[str, Set[str]]:
    """Extract property name sets from an OpenAPI operation.

    Returns {"request_fields": set, "response_fields": set}.
    """
    req_fields: Set[str] = set()
    resp_fields: Set[str] = set()

    # --- Request body ---
    req_body = operation.get("requestBody", {})
    if isinstance(req_body, dict) and req_body:
        content = req_body.get("content", {})
        if isinstance(content, dict):
            for _mt, media_obj in content.items():
                if isinstance(media_obj, dict) and "schema" in media_obj:
                    req_fields = collect_property_names(media_obj["schema"])
                    break

    # --- Response (first 2xx) ---
    responses = operation.get("responses", {})
    if isinstance(responses, dict):
        for code, resp in responses.items():
            if not str(code).startswith("2") or not isinstance(resp, dict):
                continue
            content = resp.get("content", {})
            if isinstance(content, dict):
                for _mt, media_obj in content.items():
                    if isinstance(media_obj, dict) and "schema" in media_obj:
                        resp_fields = collect_property_names(
                            media_obj["schema"]
                        )
                        break
            break  # use first 2xx only

    return {"request_fields": req_fields, "response_fields": resp_fields}
