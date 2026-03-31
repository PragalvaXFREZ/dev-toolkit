#!/usr/bin/env python3
"""
Meshery API Schema Audit

Compares data sources within meshery/meshery and meshery/schemas to produce
a comprehensive audit of every API endpoint:

  1. server/router/server.go    → registered API endpoints
  2. docs/data/openapi.yml      → schema-backed check (from meshery/schemas)
  3. server/handlers/*.go       → schema-driven check (import analysis)

For each endpoint the script computes:
  - Coverage      — Overlap / Server Underlap / Schema Underlap
  - Status        — Active / Deprecated / Unimplemented / Cloud-only
  - Schema-Backed — Is the endpoint defined in the OpenAPI spec?
  - Schema Completeness — Full / Partial / Stub / N/A
  - Schema-Driven — Does the handler import+use meshery/schemas types?

Writes results to a Google Sheet. Credentials come from environment variables.

Usage:
  python api_audit.py --repo .
  python api_audit.py --repo . --sheet-id $SHEET_ID
  python api_audit.py --repo /path/to/meshery --dry-run
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pip install pyyaml")


def load_local_env() -> None:
    """Load simple KEY=VALUE pairs from .env into os.environ."""
    seen: Set[Path] = set()
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
    ]

    for env_file in candidates:
        env_file = env_file.resolve()
        if env_file in seen or not env_file.exists():
            continue
        seen.add(env_file)

        for raw_line in env_file.read_text(errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]

            os.environ.setdefault(key, value)


load_local_env()

# ---------------------------------------------------------------------------
# Paths relative to repo root
# ---------------------------------------------------------------------------
ROUTER_FILE = "server/router/server.go"
OPENAPI_FILE = "docs/data/openapi.yml"
HANDLERS_DIR = "server/handlers"
GO_MOD_FILE = "go.mod"

# ---------------------------------------------------------------------------
# Sheet configuration
# ---------------------------------------------------------------------------
SHEET_COLUMNS = [
    "Category",
    "Sub-Category",
    "Endpoints",
    "Methods",
    "Coverage",
    "Status",
    "Schema-Backed",
    "Schema Completeness",
    "Schema-Driven",
    "Notes",
    "Change Log",
]
COL_CATEGORY = 0
COL_SUBCATEGORY = 1
COL_ENDPOINTS = 2
COL_METHODS = 3
COL_COVERAGE = 4
COL_STATUS = 5
COL_BACKED = 6
COL_COMPLETENESS = 7
COL_DRIVEN = 8
COL_NOTES = 9
COL_CHANGELOG = 10

WORKSHEET_NAME = "Verification of Meshery Server API Endpoints"

HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})

# Methods that typically carry a request body
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

MIDDLEWARE_NAMES = frozenset({
    "ProviderMiddleware", "AuthMiddleware", "SessionInjectorMiddleware",
    "KubernetesMiddleware", "K8sFSMMiddleware", "GraphqlMiddleware",
    "NoCacheMiddleware",
})

# Category classification — most-specific prefix first
CATEGORY_RULES: List[Tuple[str, str, str]] = [
    ("/api/system/graphql", "Meshery Server and Components", "Meshery Operator"),
    ("/api/system/database", "Meshery Server and Components", "Database"),
    ("/api/system/kubernetes", "Meshery Server and Components", "System"),
    ("/api/system/adapter", "Meshery Server and Components", "Adapters"),
    ("/api/system/adapters", "Meshery Server and Components", "Adapters"),
    ("/api/system/availableAdapters", "Meshery Server and Components", "Adapters"),
    ("/api/system/meshsync", "Meshery Server and Components", "Meshsync"),
    ("/api/system/events", "Meshery Server and Components", "System"),
    ("/api/system/version", "Meshery Server and Components", "System"),
    ("/api/system/sync", "Meshery Server and Components", "System"),
    ("/api/system/fileDownload", "Meshery Server and Components", "System"),
    ("/api/system/fileView", "Meshery Server and Components", "System"),
    ("/api/extension/version", "Meshery Server and Components", "System"),
    ("/api/integrations/connections", "Integrations", "Connections"),
    ("/api/integrations/credentials", "Integrations", "Credentials"),
    ("/api/environments", "Integrations", "Environments"),
    ("/api/workspaces", "Integrations", "Workspaces"),
    ("/api/meshmodels", "Capabilities Registry", "Entities"),
    ("/api/meshmodel", "Capabilities Registry", "Model Lifecycle"),
    ("/api/pattern/deploy", "Configuration", "Patterns"),
    ("/api/pattern/import", "Configuration", "Patterns"),
    ("/api/pattern/catalog", "Configuration", "Patterns"),
    ("/api/pattern/clone", "Configuration", "Patterns"),
    ("/api/pattern/download", "Configuration", "Patterns"),
    ("/api/pattern/types", "Configuration", "Patterns"),
    ("/api/pattern", "Configuration", "Patterns"),
    ("/api/patterns", "Configuration", "Patterns"),
    ("/api/filter", "Configuration", "Filters"),
    ("/api/content/design", "Configuration", "Patterns"),
    ("/api/content/filter", "Configuration", "Filters"),
    ("/api/perf", "Benchmarking and Validation", "Performance (SMP)"),
    ("/api/mesh", "Benchmarking and Validation", "Performance (SMP)"),
    ("/api/smi", "Benchmarking and Validation", "Conformance (SMI)"),
    ("/api/user/performance", "Benchmarking and Validation", "Performance (SMP)"),
    ("/api/user/prefs/perf", "Benchmarking and Validation", "Performance (SMP)"),
    ("/api/user/schedules", "Identity", "User"),
    ("/api/telemetry/metrics/grafana", "Telemetry", "Grafana API"),
    ("/api/grafana", "Telemetry", "Grafana API"),
    ("/api/telemetry/metrics", "Telemetry", "Prometheus API"),
    ("/api/prometheus", "Telemetry", "Prometheus API"),
    ("/api/identity/orgs", "Identity", "Organization"),
    ("/api/identity", "Identity", "User"),
    ("/api/user", "Identity", "User"),
    ("/api/token", "Identity", "User"),
    ("/api/provider", "Identity", "Providers, Extensions"),
    ("/api/providers", "Identity", "Providers, Extensions"),
    ("/api/extension", "Identity", "Providers, Extensions"),
    ("/api/extensions", "Identity", "Providers, Extensions"),
    ("/api/schema", "Meshery Server and Components", "System"),
    ("/provider", "Identity", "Providers, Extensions"),
    ("/auth", "Identity", "User"),
    ("/user/login", "Identity", "User"),
    ("/user/logout", "Identity", "User"),
    ("/swagger.yaml", "Meshery Server and Components", "System"),
    ("/docs", "Meshery Server and Components", "System"),
    ("/healthz", "Meshery Server and Components", "System"),
    ("/error", "Other", ""),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """Replace {paramName} with positional {p1}, {p2}, ... for matching."""
    counter = [0]

    def _repl(_m):
        counter[0] += 1
        return f"{{p{counter[0]}}}"

    return re.sub(r"\{[^}]+\}", _repl, path)


def categorize(path: str) -> Tuple[str, str]:
    """Return (category, subcategory) for a given endpoint path."""
    for prefix, cat, sub in CATEGORY_RULES:
        if path.startswith(prefix):
            return cat, sub
    return "Other", ""


def endpoint_sort_key(endpoint: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """Return a deterministic sort key for sheet output."""
    return (
        endpoint["category"],
        endpoint["subcategory"],
        endpoint["path"],
        endpoint["methods"],
    )


# ---------------------------------------------------------------------------
# 1. Router parser — server/router/server.go
# ---------------------------------------------------------------------------

def parse_router(repo: Path) -> List[Dict[str, Any]]:
    """Parse route registrations from server.go."""
    router_file = repo / ROUTER_FILE
    if not router_file.exists():
        print(f"ERROR: {router_file} not found", file=sys.stderr)
        return []

    content = router_file.read_text(errors="replace")
    lines = content.splitlines()

    # Accumulate multi-line gMux statements
    statements: List[str] = []
    current = ""
    paren_depth = 0
    in_stmt = False
    current_commented = False

    for line in lines:
        stripped = line.strip()
        if re.match(r"^\s*(//\s*)?gMux\.(Handle|HandleFunc|PathPrefix)", line):
            if current and in_stmt:
                statements.append(current)
            current = stripped
            in_stmt = True
            current_commented = stripped.startswith("//")
            paren_depth = current.count("(") - current.count(")")
            if paren_depth <= 0 and not stripped.rstrip().endswith("."):
                statements.append(current)
                current, in_stmt, paren_depth, current_commented = "", False, 0, False
            continue
        if in_stmt:
            continuation = stripped
            if current_commented:
                continuation = re.sub(r"^//\s*", "", continuation)
            current += " " + continuation
            paren_depth += continuation.count("(") - continuation.count(")")
            if paren_depth <= 0 and not continuation.rstrip().endswith("."):
                statements.append(current)
                current, in_stmt, paren_depth, current_commented = "", False, 0, False

    if current:
        statements.append(current)

    routes = []
    for stmt in statements:
        route = _parse_route(stmt)
        if route:
            routes.append(route)
    return routes


def _parse_route(stmt: str) -> Optional[Dict[str, Any]]:
    """Parse a single gMux statement into a route dict."""
    commented = stmt.lstrip().startswith("//")
    clean = re.sub(r"^//\s*", "", stmt.strip()) if commented else stmt.strip()

    path_m = re.search(
        r'gMux\.(Handle|HandleFunc|PathPrefix)\s*\(\s*"([^"]+)"', clean
    )
    if not path_m:
        return None

    path = path_m.group(2)
    methods_m = re.search(r"\.\s*Methods\(\s*(.+?)\s*\)", clean)
    methods = re.findall(r'"([A-Z]+)"', methods_m.group(1)) if methods_m else ["ALL"]
    handler = _extract_handler(clean)

    return {
        "path": path,
        "methods": sorted(methods),
        "handler": handler,
        "commented": commented,
    }


def _extract_handler(line: str) -> str:
    """Extract handler function name from a route registration line."""
    # Exported methods on Handler receiver (h.FuncName)
    refs = re.findall(r"h\.([A-Z]\w+)", line)
    actual = [r for r in refs if r not in MIDDLEWARE_NAMES]
    if actual:
        return actual[-1]

    # Any h.funcName (including unexported)
    refs = re.findall(r"h\.([A-Za-z]\w+)", line)
    actual = [r for r in refs if r not in MIDDLEWARE_NAMES]
    if actual:
        return actual[-1]

    if "func(" in line or "func (" in line:
        return "<inline>"
    return "<unknown>"


# ---------------------------------------------------------------------------
# 2. OpenAPI parser — docs/data/openapi.yml
# ---------------------------------------------------------------------------

def _has_meaningful_schema(schema: Optional[dict]) -> bool:
    """Check if a schema object has real structure beyond a bare type."""
    if not schema or not isinstance(schema, dict):
        return False
    if "$ref" in schema:
        return True
    if any(k in schema for k in ("allOf", "oneOf", "anyOf")):
        return True
    if schema.get("type") == "array" and "items" in schema:
        return True
    if schema.get("type") == "object" and "properties" in schema:
        return True
    return False


def _get_content_schema(content: Any) -> Optional[dict]:
    """Extract schema from a content map (application/json, etc.)."""
    if not isinstance(content, dict):
        return None
    for _media_type, media_obj in content.items():
        if isinstance(media_obj, dict) and "schema" in media_obj:
            return media_obj["schema"]
    return None


def _assess_completeness(operation: dict, method: str) -> Tuple[str, List[str]]:
    """Assess schema completeness for a single OpenAPI operation.

    Returns (completeness, detail_notes).
    """
    notes: List[str] = []
    expects_body = method.upper() in BODY_METHODS

    # --- Request side ---
    request_meaningful = False
    req_body = operation.get("requestBody", {})
    if isinstance(req_body, dict):
        if "$ref" in req_body:
            request_meaningful = True
        else:
            req_schema = _get_content_schema(req_body.get("content", {}))
            request_meaningful = _has_meaningful_schema(req_schema)

    # --- Response side ---
    response_meaningful = False
    responses = operation.get("responses", {})
    if isinstance(responses, dict):
        for code, resp in responses.items():
            if not str(code).startswith("2") or not isinstance(resp, dict):
                continue
            if "$ref" in resp:
                response_meaningful = True
                break
            resp_schema = _get_content_schema(resp.get("content", {}))
            if _has_meaningful_schema(resp_schema):
                response_meaningful = True
                break

    # --- Classify ---
    if expects_body:
        if request_meaningful and response_meaningful:
            return "Full", notes
        if request_meaningful or response_meaningful:
            if not request_meaningful:
                notes.append("spec missing requestBody schema")
            if not response_meaningful:
                notes.append("spec missing response schema")
            return "Partial", notes
        notes.append("spec entry has no request/response schemas")
        return "Stub", notes
    else:
        # GET, DELETE, HEAD, OPTIONS — only response matters
        if response_meaningful:
            return "Full", notes
        notes.append("spec entry has no response schema")
        return "Stub", notes


def parse_openapi(repo: Path) -> dict:
    """Parse openapi.yml and return structured data for all columns.

    Returns a dict with:
      all_paths:      {norm_path: {METHOD, ...}}
      completeness:   {(norm_path, METHOD): "Full"/"Partial"/"Stub"}
      x_internal:     {(norm_path, METHOD): ["cloud"] or []}
      original_paths: {norm_path: original_path}
      compl_notes:    {(norm_path, METHOD): [detail_notes]}
    """
    spec_file = repo / OPENAPI_FILE
    empty = {
        "all_paths": {},
        "completeness": {},
        "x_internal": {},
        "original_paths": {},
        "compl_notes": {},
    }
    if not spec_file.exists():
        print(f"ERROR: {spec_file} not found", file=sys.stderr)
        return empty

    with open(spec_file, encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    all_paths: Dict[str, Set[str]] = {}
    completeness: Dict[Tuple[str, str], str] = {}
    x_internal: Dict[Tuple[str, str], List[str]] = {}
    original_paths: Dict[str, str] = {}
    compl_notes: Dict[Tuple[str, str], List[str]] = {}

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

            # Track original path for spec-only endpoints
            if norm not in original_paths:
                original_paths[norm] = path

            # x-internal tag
            xi = details.get("x-internal", [])
            if not isinstance(xi, list):
                xi = [xi] if xi else []
            x_internal[(norm, m_upper)] = xi

            # Schema completeness
            comp, cnotes = _assess_completeness(details, method)
            completeness[(norm, m_upper)] = comp
            compl_notes[(norm, m_upper)] = cnotes

    return {
        "all_paths": all_paths,
        "completeness": completeness,
        "x_internal": x_internal,
        "original_paths": original_paths,
        "compl_notes": compl_notes,
    }


# ---------------------------------------------------------------------------
# 3. Schema-driven detector — server/handlers/*.go
# ---------------------------------------------------------------------------

def _extract_function_body(text: str, func_name: str) -> Optional[str]:
    """Extract the body of a Go function using brace-depth counting.

    Skips braces inside string literals to avoid miscounting.
    """
    pat = re.compile(
        rf"func\s+(?:\([^)]*\)\s+)?{re.escape(func_name)}\s*\("
    )
    m = pat.search(text)
    if not m:
        return None

    brace_pos = text.find("{", m.end())
    if brace_pos == -1:
        return None

    depth = 1
    i = brace_pos + 1
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                i += 1
        elif ch == "`":
            i += 1
            while i < len(text) and text[i] != "`":
                i += 1
        i += 1

    return text[brace_pos + 1 : i - 1] if depth == 0 else None


def build_schema_driven_map(repo: Path) -> Dict[str, Tuple[str, str]]:
    """Scan handler files for meshery/schemas imports at function level.

    For each handler function, extracts its body and checks which schema
    import aliases are actually used inside it — not just present in the file.

    Returns {handler_name: (status, reason)} where status is:
      TRUE    — function uses versioned schema types (models/v1beta1/*, etc.)
      Partial — function uses only models/core (utility types)
      FALSE   — function does not use any schema imports
    """
    handlers_dir = repo / HANDLERS_DIR
    if not handlers_dir.exists():
        print(f"WARNING: {handlers_dir} not found", file=sys.stderr)
        return {}

    # Read schema module path from go.mod
    schema_module = "github.com/meshery/schemas"
    go_mod = repo / GO_MOD_FILE
    if go_mod.exists():
        for line in go_mod.read_text().splitlines():
            m = re.match(r"\s*(github\.com/meshery/schemas)\s+v[\d.]+", line.strip())
            if m:
                schema_module = m.group(1)
                break

    escaped = re.escape(schema_module)
    alias_pat = re.compile(rf'(\w+)\s+"({escaped}[^"]*)"')
    bare_pat = re.compile(rf'"({escaped}[^"]*)"')

    # Per-file data
    handler_to_file: Dict[str, str] = {}
    file_texts: Dict[str, str] = {}
    file_aliases: Dict[str, Dict[str, str]] = {}  # fpath → {alias: import_path}

    for go_file in sorted(handlers_dir.glob("*.go")):
        if go_file.name.endswith("_test.go"):
            continue

        text = go_file.read_text(errors="replace")
        fpath = str(go_file)
        file_texts[fpath] = text

        # Map handler names → file
        for name in re.findall(
            r"func\s+\([^)]*\*?Handler[^)]*\)\s+(\w+)\s*\(", text
        ):
            handler_to_file[name] = fpath
        for name in re.findall(r"^func\s+(\w+)\s*\(", text, re.MULTILINE):
            if name not in handler_to_file:
                handler_to_file[name] = fpath

        # Build alias map: alias → full import path
        aliases: Dict[str, str] = {}
        for alias, imp_path in alias_pat.findall(text):
            aliases[alias] = imp_path
        seen_paths = set(aliases.values())
        for imp_path in bare_pat.findall(text):
            if imp_path not in seen_paths:
                last_seg = imp_path.rstrip("/").rsplit("/", 1)[-1]
                aliases[last_seg] = imp_path
                seen_paths.add(imp_path)
        file_aliases[fpath] = aliases

    # Classify each handler at function level
    result: Dict[str, Tuple[str, str]] = {}
    for name, fpath in handler_to_file.items():
        aliases = file_aliases.get(fpath, {})
        text = file_texts.get(fpath, "")

        # No schema imports in this file at all → fast path
        if not aliases:
            result[name] = ("FALSE", "no schema imports")
            continue

        # Try function-level analysis
        func_body = _extract_function_body(text, name)
        if func_body is not None:
            used: Set[str] = set()
            for alias, imp_path in aliases.items():
                if re.search(rf"\b{re.escape(alias)}\.", func_body):
                    used.add(imp_path)

            if used:
                versioned = {p for p in used if re.search(r"models/v\d+", p)}
                core_only = {p for p in used if "models/core" in p}
                if versioned:
                    pkgs = ", ".join(
                        sorted(p.replace(schema_module + "/", "") for p in versioned)
                    )
                    result[name] = ("TRUE", f"imports: {pkgs}")
                elif core_only:
                    result[name] = ("Partial", "imports: models/core only")
                else:
                    result[name] = ("FALSE", "schema dep but no model types")
            else:
                result[name] = ("FALSE", "no schema usage in function body")
        else:
            # Couldn't extract body — fall back to file-level
            all_imports = set(aliases.values())
            versioned = {p for p in all_imports if re.search(r"models/v\d+", p)}
            core_only = {p for p in all_imports if "models/core" in p}
            if versioned:
                pkgs = ", ".join(
                    sorted(p.replace(schema_module + "/", "") for p in versioned)
                )
                result[name] = ("TRUE", f"imports: {pkgs} (file-level)")
            elif core_only:
                result[name] = ("Partial", "imports: models/core only (file-level)")
            else:
                result[name] = ("FALSE", "schema dep but no model types")

    return result


# ---------------------------------------------------------------------------
# 4. Classification — bidirectional walk (Router ∪ Spec)
# ---------------------------------------------------------------------------

def _aggregate_completeness(
    norm: str,
    methods: List[str],
    spec_data: dict,
) -> Tuple[str, List[str]]:
    """Aggregate completeness across methods for a single endpoint."""
    comp_map = spec_data["completeness"]
    cnotes_map = spec_data["compl_notes"]
    all_paths = spec_data["all_paths"]
    spec_methods = all_paths.get(norm, set())

    if not spec_methods:
        return "N/A", []

    method_comps = []
    agg_notes: List[str] = []
    check_methods = spec_methods if methods == ["ALL"] else [
        m for m in methods if m in spec_methods
    ]

    for m in check_methods:
        c = comp_map.get((norm, m), "Stub")
        method_comps.append(c)
        agg_notes.extend(cnotes_map.get((norm, m), []))

    if not method_comps:
        return "Stub", ["spec path exists but no method match"]

    # Deduplicate notes
    seen: Set[str] = set()
    unique_notes = []
    for n in agg_notes:
        if n not in seen:
            seen.add(n)
            unique_notes.append(n)

    if all(c == "Full" for c in method_comps):
        return "Full", unique_notes
    if any(c == "Full" for c in method_comps) or any(c == "Partial" for c in method_comps):
        return "Partial", unique_notes
    return "Stub", unique_notes


def classify_endpoints(
    routes: List[Dict[str, Any]],
    spec_data: dict,
    schema_map: Dict[str, Tuple[str, str]],
) -> List[Dict[str, Any]]:
    """Classify endpoints from both router and spec (bidirectional walk)."""
    all_paths = spec_data["all_paths"]
    x_internal_map = spec_data["x_internal"]
    original_paths = spec_data["original_paths"]

    endpoints: List[Dict[str, Any]] = []
    router_norm_paths: Set[str] = set()

    # ------------------------------------------------------------------
    # Pass 1: Router-sourced endpoints
    # ------------------------------------------------------------------
    grouped_routes: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for route in routes:
        methods_str = ", ".join(route["methods"])
        grouped_routes[(route["path"], methods_str)].append(route)

    for path, methods_str in sorted(grouped_routes):
        route_group = grouped_routes[(path, methods_str)]
        route = next((r for r in route_group if not r["commented"]), route_group[0])
        methods = route["methods"]
        is_commented = all(r["commented"] for r in route_group)

        category, subcategory = categorize(path)
        norm = normalize_path(path)
        router_norm_paths.add(norm)

        spec_methods = all_paths.get(norm, set())

        # --- Coverage ---
        coverage = "Overlap" if spec_methods else "Server Underlap"

        # --- Status ---
        status = "Deprecated" if is_commented else "Active"

        # --- Schema-Backed ---
        backed = "TRUE" if spec_methods else "FALSE"

        # --- Schema Completeness ---
        completeness, compl_notes = _aggregate_completeness(norm, methods, spec_data)

        # --- Schema-Driven ---
        handler = route["handler"]
        if handler in ("<inline>", "<unknown>"):
            driven, driven_reason = "FALSE", f"handler: {handler}"
        else:
            driven, driven_reason = schema_map.get(
                handler, ("FALSE", "handler not mapped")
            )

        # --- Notes ---
        notes: List[str] = []
        if is_commented:
            notes.append("commented out in server.go")
        if coverage == "Server Underlap":
            notes.append("no path in OpenAPI spec")
        else:
            # Check if any matched method is cloud-only
            cloud_methods = []
            for m in (spec_methods if methods == ["ALL"] else methods):
                xi = x_internal_map.get((norm, m), [])
                if "cloud" in xi:
                    cloud_methods.append(m)
            if cloud_methods and len(cloud_methods) == len(spec_methods):
                notes.append("spec marked cloud-only (x-internal)")
        if compl_notes:
            notes.extend(compl_notes)
        if driven_reason:
            notes.append(driven_reason)

        endpoints.append({
            "category": category,
            "subcategory": subcategory,
            "path": path,
            "methods": methods_str,
            "coverage": coverage,
            "status": status,
            "backed": backed,
            "completeness": completeness,
            "driven": driven,
            "notes": "; ".join(notes),
        })

    # ------------------------------------------------------------------
    # Pass 2: Spec-only endpoints (Schema Underlap)
    # ------------------------------------------------------------------
    for norm_path, spec_methods in sorted(all_paths.items()):
        if norm_path in router_norm_paths:
            continue

        original = original_paths.get(norm_path, norm_path)
        methods_sorted = sorted(spec_methods)
        category, subcategory = categorize(original)

        # Determine x-internal across all methods for this path
        all_cloud = True
        any_cloud = False
        for m in methods_sorted:
            xi = x_internal_map.get((norm_path, m), [])
            if "cloud" in xi:
                any_cloud = True
            else:
                all_cloud = False

        # --- Coverage ---
        coverage = "Schema Underlap"

        # --- Status ---
        if all_cloud:
            status = "Cloud-only"
        else:
            status = "Unimplemented"

        # --- Schema-Backed ---
        backed = "TRUE"

        # --- Schema Completeness ---
        completeness, compl_notes = _aggregate_completeness(
            norm_path, methods_sorted, spec_data
        )

        # --- Schema-Driven ---
        driven = "N/A"

        # --- Notes ---
        notes: List[str] = []
        if status == "Cloud-only":
            notes.append("defined in spec; cloud-only (x-internal)")
        elif any_cloud:
            notes.append("defined in spec; partially cloud-only (x-internal)")
        else:
            notes.append("defined in spec; no server route")
        if compl_notes:
            notes.extend(compl_notes)
        notes.append("no handler (schema-only endpoint)")

        endpoints.append({
            "category": category,
            "subcategory": subcategory,
            "path": original,
            "methods": ", ".join(methods_sorted),
            "coverage": coverage,
            "status": status,
            "backed": backed,
            "completeness": completeness,
            "driven": driven,
            "notes": "; ".join(notes),
        })

    return sorted(endpoints, key=endpoint_sort_key)


# ---------------------------------------------------------------------------
# Google Sheet — credentials from environment, never hardcoded
# ---------------------------------------------------------------------------

def _get_sheet_client():
    """Authenticate with Google Sheets using env-var credentials."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        sys.exit(
            "Missing packages. Run: pip install gspread google-auth"
        )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    # Option 1: inline JSON (GitHub Actions secrets)
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(creds)

    # Option 2: file path (local development)
    creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_file and os.path.exists(creds_file):
        creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
        return gspread.authorize(creds)

    return None


def _col_letter(idx: int) -> str:
    """Convert 0-based column index to sheet column letter (A, B, ... Z, AA, ...)."""
    result = ""
    while True:
        result = chr(65 + idx % 26) + result
        idx = idx // 26 - 1
        if idx < 0:
            break
    return result


# Columns the script compares and updates on matched rows.
# (column_index, endpoint_dict_key, human_label)
_UPDATABLE_COLUMNS = [
    (COL_COVERAGE, "coverage", "coverage"),
    (COL_STATUS, "status", "status"),
    (COL_BACKED, "backed", "backed"),
    (COL_COMPLETENESS, "completeness", "completeness"),
    (COL_DRIVEN, "driven", "driven"),
    (COL_NOTES, "notes", "notes"),
]


def update_sheet(
    endpoints: List[Dict[str, Any]],
    sheet_id: str,
    dry_run: bool = False,
) -> List[str]:
    """Diff computed endpoints against the sheet and apply updates.

    - Matches rows by normalized endpoint path + method overlap.
    - Updates Coverage, Status, Schema-Backed, Schema Completeness,
      Schema-Driven, and Notes columns when they differ.
    - Inserts new rows into matching category groups when possible.
    - Stamps the Change Log column on modified rows.
    """
    gc = _get_sheet_client()
    if not gc:
        print(
            "ERROR: No credentials found.\n"
            "  Set GOOGLE_CREDENTIALS_JSON (inline JSON for CI) or\n"
            "  GOOGLE_APPLICATION_CREDENTIALS (file path for local dev).",
            file=sys.stderr,
        )
        sys.exit(1)

    sheet = gc.open_by_key(sheet_id)
    ws = sheet.get_worksheet(4)

    print(f"Connected to worksheet: {ws.title}")
    current_rows = ws.get_all_values()

    # Index sheet rows by normalized path
    sheet_index: Dict[str, List[Tuple[int, Set[str]]]] = defaultdict(list)
    for idx, row in enumerate(current_rows):
        if idx == 0:
            continue
        ep = row[COL_ENDPOINTS].strip() if len(row) > COL_ENDPOINTS else ""
        if not ep:
            continue
        if not ep.startswith("/"):
            ep = "/" + ep
        norm = normalize_path(ep)
        raw_methods = row[COL_METHODS].strip() if len(row) > COL_METHODS else ""
        mset = {
            m.strip().upper()
            for m in raw_methods.replace(";", ",").split(",")
            if m.strip()
        }
        sheet_index[norm].append((idx, mset))

    changes: List[str] = []
    batch_updates: List[Dict[str, Any]] = []
    new_rows_info: List[Tuple[List[str], str, str]] = []
    matched_rows: Set[int] = set()
    today = date.today().isoformat()

    for ep in endpoints:
        norm = normalize_path(ep["path"])
        ep_mset = {m.strip() for m in ep["methods"].split(",")}
        candidates = sheet_index.get(norm, [])

        # Find matching sheet row
        matched_idx = None
        for idx, sheet_mset in candidates:
            if idx in matched_rows:
                continue
            if (
                "ALL" in ep_mset
                or "ALL" in sheet_mset
                or ep_mset & sheet_mset
                or not sheet_mset
                or not ep_mset
            ):
                matched_idx = idx
                break

        if matched_idx is not None:
            matched_rows.add(matched_idx)
            row = current_rows[matched_idx]
            while len(row) < len(SHEET_COLUMNS):
                row.append("")

            row_changed = False
            for col_idx, field, label in _UPDATABLE_COLUMNS:
                old_val = row[col_idx].strip() if len(row) > col_idx else ""
                new_val = ep[field]
                if old_val != new_val:
                    cl = _col_letter(col_idx)
                    changes.append(
                        f"UPDATE row {matched_idx + 1} [{ep['path']}] "
                        f"{label}: '{old_val}' -> '{new_val}'"
                    )
                    batch_updates.append({
                        "range": f"{cl}{matched_idx + 1}",
                        "values": [[new_val]],
                    })
                    row_changed = True

            if row_changed:
                cl = _col_letter(COL_CHANGELOG)
                batch_updates.append({
                    "range": f"{cl}{matched_idx + 1}",
                    "values": [[today]],
                })
        else:
            new_row = [
                ep["category"],
                ep["subcategory"],
                ep["path"],
                ep["methods"],
                ep["coverage"],
                ep["status"],
                ep["backed"],
                ep["completeness"],
                ep["driven"],
                ep["notes"],
                today,
            ]
            changes.append(
                f"NEW ROW: {ep['path']} [{ep['methods']}] "
                f"coverage={ep['coverage']} status={ep['status']} "
                f"backed={ep['backed']} completeness={ep['completeness']} "
                f"driven={ep['driven']}"
            )
            new_rows_info.append((new_row, ep["category"], ep["subcategory"]))

    new_rows_info.sort(
        key=lambda item: endpoint_sort_key({
            "category": item[1],
            "subcategory": item[2],
            "path": item[0][COL_ENDPOINTS],
            "methods": item[0][COL_METHODS],
        })
    )

    # --- Apply batch cell updates ---
    if not dry_run and batch_updates:
        try:
            ws.batch_update(batch_updates, value_input_option="RAW")
            print(f"Batch updated {len(batch_updates)} cells")
        except Exception as exc:
            changes.append(f"BATCH UPDATE ERROR: {exc}")

    # --- Insert new rows ---
    if not dry_run and new_rows_info:
        _insert_rows_by_group(ws, new_rows_info, changes)

    return changes


def _insert_rows_by_group(
    ws,
    new_rows_info: List[Tuple[List[str], str, str]],
    changes: List[str],
) -> None:
    """Insert new rows into the correct category/sub-category block.

    Groups insertions by target position and processes from bottom to top
    so that earlier inserts don't shift indices for later ones.
    """
    try:
        all_rows = ws.get_all_values()
    except Exception as exc:
        changes.append(f"INSERT ERROR (read failed): {exc}")
        return

    # Build index: last row for each (category, subcategory) and category
    group_last_row: Dict[Tuple[str, str], int] = {}
    cat_last_row: Dict[str, int] = {}
    last_cat = ""
    last_sub = ""

    for idx, row in enumerate(all_rows):
        if idx == 0:
            continue

        cat = row[COL_CATEGORY].strip() if len(row) > COL_CATEGORY else ""
        sub = row[COL_SUBCATEGORY].strip() if len(row) > COL_SUBCATEGORY else ""

        if cat:
            last_cat = cat
        else:
            cat = last_cat

        if sub:
            last_sub = sub
        else:
            sub = last_sub

        if cat:
            group_last_row[(cat, sub)] = idx
            cat_last_row[cat] = idx

    # Classify each new row: targeted insert or append
    inserts: List[Tuple[int, List[str]]] = []
    append_rows: List[List[str]] = []

    for row_data, cat, sub in new_rows_info:
        target = group_last_row.get((cat, sub))
        if target is None:
            target = cat_last_row.get(cat)

        if target is not None:
            inserts.append((target, row_data))
            # Advance the group pointer so subsequent rows in the same
            # group land after this one rather than on top of it.
            group_last_row[(cat, sub)] = target + 1
            cat_last_row[cat] = target + 1
        else:
            append_rows.append(row_data)

    # Insert from bottom to top to preserve indices
    inserts.sort(key=lambda item: item[0], reverse=True)

    for insert_after, row_data in inserts:
        try:
            ws.insert_row(row_data, insert_after + 2, value_input_option="RAW")
        except Exception as exc:
            changes.append(f"INSERT ERROR at row {insert_after + 2}: {exc}")

    if append_rows:
        try:
            ws.append_rows(append_rows, value_input_option="RAW")
        except Exception as exc:
            changes.append(f"APPEND ROWS ERROR: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Audit Meshery API endpoints for schema coverage, completeness, "
            "and schema-driven status. Writes results to Google Sheet."
        )
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("MESHERY_REPO_PATH", "."),
        help=(
            "Path to the meshery/meshery repo root "
            "(default: cwd or $MESHERY_REPO_PATH)"
        ),
    )
    parser.add_argument(
        "--sheet-id",
        default=os.environ.get("SHEET_ID"),
        help="Google Sheet ID (or set $SHEET_ID env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print diff without writing to the sheet",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print per-endpoint details",
    )
    args = parser.parse_args()
    repo = Path(args.repo).resolve()

    # Validate
    if not (repo / ROUTER_FILE).exists():
        print(
            f"ERROR: {ROUTER_FILE} not found in {repo}\n"
            "Use --repo to point to the meshery/meshery repo root.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Phase 1: Parse ---
    print("Parsing router...")
    routes = parse_router(repo)
    print(f"  {len(routes)} route registrations")

    print("Parsing OpenAPI spec...")
    spec_data = parse_openapi(repo)
    n_spec = len(spec_data["all_paths"])
    print(f"  {n_spec} spec paths")

    print("Scanning handler imports...")
    schema_map = build_schema_driven_map(repo)
    n_true = sum(1 for s, _ in schema_map.values() if s == "TRUE")
    n_part = sum(1 for s, _ in schema_map.values() if s == "Partial")
    print(f"  {len(schema_map)} handlers ({n_true} schema-driven, {n_part} partial)")

    # --- Phase 2: Classify ---
    endpoints = classify_endpoints(routes, spec_data, schema_map)
    total = len(endpoints)

    # Coverage breakdown
    n_overlap = sum(1 for e in endpoints if e["coverage"] == "Overlap")
    n_srv_under = sum(1 for e in endpoints if e["coverage"] == "Server Underlap")
    n_sch_under = sum(1 for e in endpoints if e["coverage"] == "Schema Underlap")

    # Status breakdown
    n_active = sum(1 for e in endpoints if e["status"] == "Active")
    n_deprecated = sum(1 for e in endpoints if e["status"] == "Deprecated")
    n_unimpl = sum(1 for e in endpoints if e["status"] == "Unimplemented")
    n_cloud = sum(1 for e in endpoints if e["status"] == "Cloud-only")

    # Schema-Backed
    b_true = sum(1 for e in endpoints if e["backed"] == "TRUE")

    # Completeness
    comp_full = sum(1 for e in endpoints if e["completeness"] == "Full")
    comp_part = sum(1 for e in endpoints if e["completeness"] == "Partial")
    comp_stub = sum(1 for e in endpoints if e["completeness"] == "Stub")

    # Schema-Driven
    d_true = sum(1 for e in endpoints if e["driven"] == "TRUE")
    d_part = sum(1 for e in endpoints if e["driven"] == "Partial")

    print(f"\nClassified {total} endpoints:")
    print(f"  Coverage:      {n_overlap} Overlap, {n_srv_under} Server Underlap, {n_sch_under} Schema Underlap")
    print(f"  Status:        {n_active} Active, {n_deprecated} Deprecated, {n_unimpl} Unimplemented, {n_cloud} Cloud-only")
    print(f"  Backed:        {b_true} TRUE, {total - b_true} FALSE")
    print(f"  Completeness:  {comp_full} Full, {comp_part} Partial, {comp_stub} Stub")
    print(f"  Driven:        {d_true} TRUE, {d_part} Partial")

    if args.verbose:
        print()
        for ep in endpoints:
            print(
                f"  {ep['path']:55s} [{ep['methods']:20s}] "
                f"cov={ep['coverage']:16s} st={ep['status']:14s} "
                f"bk={ep['backed']:5s} comp={ep['completeness']:7s} "
                f"drv={ep['driven']:7s}"
            )

    # --- Phase 3: Update sheet ---
    if not args.sheet_id:
        print(
            "\nNo --sheet-id provided. Set $SHEET_ID or pass --sheet-id "
            "to write results to Google Sheet."
        )
        sys.exit(0)

    label = "DRY RUN — previewing" if args.dry_run else "Updating"
    print(f"\n{label} Google Sheet...")

    changes = update_sheet(endpoints, args.sheet_id, args.dry_run)

    if not changes:
        print("Sheet is up to date.")
    else:
        print(f"\n{len(changes)} change(s):")
        for ch in changes:
            print(f"  {ch}")


if __name__ == "__main__":
    main()
