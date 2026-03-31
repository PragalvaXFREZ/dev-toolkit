#!/usr/bin/env python3
"""
Meshery API Schema Audit

Compares three data sources within the meshery/meshery repo:
  1. server/router/server.go    → registered API endpoints
  2. docs/data/openapi.yml      → schema-codified check (excludes cloud-only)
  3. server/handlers/*.go       → schema-driven check (import analysis)

Writes results to a Google Sheet. Credentials are loaded from environment
variables — never hardcoded.

If a local .env file is present, it is loaded automatically.

Usage:
  # Local (from meshery repo root)
  python api_audit.py --repo .

  # CI
  python api_audit.py --repo . --sheet-id $SHEET_ID

  # Preview without writing
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
    "API Endpoint is codifed within a schema defined in meshery/schemas.",
    "Schema-driven in Meshery Server (Models are imported into Meshery Server "
    "from meshery/schemas and used; they are not locally defined in repo)",
    "Notes",
    "Change Log",
]
COL_CATEGORY = 0
COL_SUBCATEGORY = 1
COL_ENDPOINTS = 2
COL_METHODS = 3
COL_CODIFIED = 4
COL_DRIVEN = 5
COL_NOTES = 6
COL_CHANGELOG = 7

WORKSHEET_NAME = "Verification of Meshery Server API Endpoints"

HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})

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

def parse_openapi(repo: Path) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Parse openapi.yml and return two lookups:

    1. all_paths:    {normalized_path: {METHOD}} — every path in the spec.
                     Used for schema-codified check (does a schema *exist*?).
    2. server_paths:  {normalized_path: {METHOD}} — excluding x-internal: cloud.
                     Used for informational notes only.

    Schema-codified answers "is it defined in meshery/schemas?" — yes even if
    the spec marks it cloud-only, because the schema definition still exists.
    """
    spec_file = repo / OPENAPI_FILE
    if not spec_file.exists():
        print(f"ERROR: {spec_file} not found", file=sys.stderr)
        return {}, {}

    with open(spec_file, encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    all_paths: Dict[str, Set[str]] = {}
    server_paths: Dict[str, Set[str]] = {}

    for path, methods_obj in doc.get("paths", {}).items():
        if not isinstance(methods_obj, dict):
            continue
        for method, details in methods_obj.items():
            if method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(details, dict):
                continue
            norm = normalize_path(path)
            all_paths.setdefault(norm, set()).add(method.upper())

            x_internal = details.get("x-internal", [])
            if "cloud" not in x_internal:
                server_paths.setdefault(norm, set()).add(method.upper())

    return all_paths, server_paths


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
# Classification
# ---------------------------------------------------------------------------

def classify_endpoints(
    routes: List[Dict[str, Any]],
    all_paths: Dict[str, Set[str]],
    server_paths: Dict[str, Set[str]],
    schema_map: Dict[str, Tuple[str, str]],
) -> List[Dict[str, Any]]:
    """Classify each route as schema-codified and schema-driven."""
    endpoints: List[Dict[str, Any]] = []
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

        # Schema-codified: prefer server-applicable schema definitions.
        # Cloud-only schema coverage counts as Partial rather than TRUE.
        spec_methods = all_paths.get(norm, set())
        server_methods = server_paths.get(norm, set())
        if methods == ["ALL"]:
            if not spec_methods:
                codified = "FALSE"
            elif server_methods:
                codified = "TRUE"
            else:
                codified = "Partial"
        else:
            matched_all = [m for m in methods if m in spec_methods]
            matched_server = [m for m in methods if m in server_methods]
            if len(matched_server) == len(methods) and matched_server:
                codified = "TRUE"
            elif matched_all:
                codified = "Partial"
            else:
                codified = "FALSE"

        # Schema-driven: handler imports schema types?
        handler = route["handler"]
        if handler in ("<inline>", "<unknown>"):
            driven, reason = "FALSE", f"handler: {handler}"
        else:
            driven, reason = schema_map.get(handler, ("FALSE", "handler not mapped"))

        # Notes
        notes: List[str] = []
        if is_commented:
            notes.append("commented out / legacy")
        if not spec_methods:
            notes.append("no path in OpenAPI spec")
        elif norm not in server_paths:
            notes.append("schema exists but marked cloud-only")
        if reason:
            notes.append(reason)

        endpoints.append({
            "category": category,
            "subcategory": subcategory,
            "path": path,
            "methods": methods_str,
            "codified": codified,
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


def update_sheet(
    endpoints: List[Dict[str, Any]],
    sheet_id: str,
    dry_run: bool = False,
) -> List[str]:
    """Diff computed endpoints against the sheet and apply updates.

    - Matches rows by normalized endpoint path + method overlap.
    - Updates columns E (codified), F (driven), and G (notes) when they differ.
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
    # try:
    #     ws = sheet.worksheet(WORKSHEET_NAME)
    # except Exception:
    #     ws = sheet.get_worksheet(4)
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
            # Match if: method overlap, either side is ALL, or either is empty
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
            # Update existing row if values differ
            matched_rows.add(matched_idx)
            row = current_rows[matched_idx]
            while len(row) < len(SHEET_COLUMNS):
                row.append("")

            row_changed = False

            old_cod = row[COL_CODIFIED].strip()
            if old_cod != ep["codified"]:
                col_letter = chr(65 + COL_CODIFIED)
                changes.append(
                    f"UPDATE row {matched_idx + 1} [{ep['path']}] "
                    f"codified: '{old_cod}' -> '{ep['codified']}'"
                )
                batch_updates.append({
                    "range": f"{col_letter}{matched_idx + 1}",
                    "values": [[ep["codified"]]],
                })
                row_changed = True

            old_drv = row[COL_DRIVEN].strip()
            if old_drv != ep["driven"]:
                col_letter = chr(65 + COL_DRIVEN)
                changes.append(
                    f"UPDATE row {matched_idx + 1} [{ep['path']}] "
                    f"driven: '{old_drv}' -> '{ep['driven']}'"
                )
                batch_updates.append({
                    "range": f"{col_letter}{matched_idx + 1}",
                    "values": [[ep["driven"]]],
                })
                row_changed = True

            old_notes = row[COL_NOTES].strip()
            if old_notes != ep["notes"]:
                col_letter = chr(65 + COL_NOTES)
                changes.append(
                    f"UPDATE row {matched_idx + 1} [{ep['path']}] "
                    f"notes: '{old_notes}' -> '{ep['notes']}'"
                )
                batch_updates.append({
                    "range": f"{col_letter}{matched_idx + 1}",
                    "values": [[ep["notes"]]],
                })
                row_changed = True

            if row_changed:
                col_letter = chr(65 + COL_CHANGELOG)
                batch_updates.append({
                    "range": f"{col_letter}{matched_idx + 1}",
                    "values": [[today]],
                })
        else:
            # New endpoint — insert into matching category group if possible
            new_row = [
                ep["category"],
                ep["subcategory"],
                ep["path"],
                ep["methods"],
                ep["codified"],
                ep["driven"],
                ep["notes"],
                today,
            ]
            changes.append(
                f"NEW ROW: {ep['path']} [{ep['methods']}] "
                f"codified={ep['codified']} driven={ep['driven']}"
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

    # Batch-apply cell updates
    if not dry_run and batch_updates:
        try:
            ws.batch_update(batch_updates, value_input_option="RAW")
            print(f"Batch updated {len(batch_updates)} cells")
        except Exception as exc:
            changes.append(f"BATCH UPDATE ERROR: {exc}")

    if not dry_run and new_rows_info:
        _insert_rows_by_group(ws, new_rows_info, changes)

    return changes


def _insert_rows_by_group(
    ws,
    new_rows_info: List[Tuple[List[str], str, str]],
    changes: List[str],
) -> None:
    """Insert rows into an existing category/sub-category block when possible."""
    try:
        all_rows = ws.get_all_values()
    except Exception as exc:
        changes.append(f"INSERT ERROR (read failed): {exc}")
        return

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

    inserts: List[Tuple[int, List[str]]] = []
    append_rows: List[List[str]] = []

    for row_data, cat, sub in new_rows_info:
        insert_after = group_last_row.get((cat, sub))
        if insert_after is None:
            insert_after = cat_last_row.get(cat)

        if insert_after is not None:
            inserts.append((insert_after, row_data))
            group_last_row[(cat, sub)] = insert_after + 1
            cat_last_row[cat] = insert_after + 1
        else:
            append_rows.append(row_data)

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
            "Audit Meshery API endpoints for schema-codified and "
            "schema-driven status. Writes results to Google Sheet."
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
    all_paths, server_paths = parse_openapi(repo)
    print(f"  {len(all_paths)} total spec paths, {len(server_paths)} server-applicable")

    print("Scanning handler imports...")
    schema_map = build_schema_driven_map(repo)
    n_true = sum(1 for s, _ in schema_map.values() if s == "TRUE")
    n_part = sum(1 for s, _ in schema_map.values() if s == "Partial")
    print(f"  {len(schema_map)} handlers ({n_true} schema-driven, {n_part} partial)")

    # --- Phase 2: Classify ---
    endpoints = classify_endpoints(routes, all_paths, server_paths, schema_map)
    total = len(endpoints)
    c_true = sum(1 for e in endpoints if e["codified"] == "TRUE")
    c_part = sum(1 for e in endpoints if e["codified"] == "Partial")
    d_true = sum(1 for e in endpoints if e["driven"] == "TRUE")
    d_part = sum(1 for e in endpoints if e["driven"] == "Partial")

    print(f"\nClassified {total} endpoints:")
    print(f"  Codified:  {c_true} TRUE, {c_part} Partial, {total - c_true - c_part} FALSE")
    print(f"  Driven:    {d_true} TRUE, {d_part} Partial, {total - d_true - d_part} FALSE")

    if args.verbose:
        print()
        for ep in endpoints:
            print(
                f"  {ep['path']:55s} [{ep['methods']:20s}] "
                f"cod={ep['codified']:7s} drv={ep['driven']:7s}"
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
