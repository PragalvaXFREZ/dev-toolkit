# Meshery API Endpoint Audit

This project audits Meshery API endpoints across the Meshery server and Meshery Cloud codebases. It combines a Python reporting pipeline with a Go AST analyzer to compare registered routes, OpenAPI coverage, schema completeness, and whether handlers are actually using `meshery/schemas` types.

The goal is to make API schema adoption measurable instead of anecdotal. The script produces a terminal summary for local review and can optionally sync the results to a Google Sheet used for ongoing verification.

## Why this matters

For recruiters and reviewers, this script demonstrates:

- Cross-repository analysis across `meshery/meshery` and `meshery-cloud`
- Static analysis with Go AST instead of fragile text-only matching
- OpenAPI parsing and endpoint coverage classification
- Schema completeness checks for request and response definitions
- Optional Google Sheets integration for stakeholder-friendly reporting
- A maintainable Python package structure with separate modules for routing, OpenAPI parsing, classification, reporting, and sheet updates

## What the audit reports

For every endpoint it can discover, the audit reports:

| Field | Meaning |
|---|---|
| Coverage | Whether an endpoint appears in the router, the OpenAPI spec, or both |
| Endpoint Status | Whether the endpoint is active, deprecated, unimplemented, or cloud-only |
| Schema-Backed | Whether the endpoint exists in the bundled OpenAPI spec |
| Schema Completeness | Whether the OpenAPI definition is full, partial, stubbed, or not available |
| Schema-Driven | Whether the Go handler imports and uses `meshery/schemas` types |
| Notes | Context for gaps, mismatches, or classification decisions |

## Repository layout

```text
meshery/api-audit/
  scripts/
    api-audit.py                  # CLI entry point
    analyze_handlers/main.go      # Go AST helper used by the Python pipeline
    audit/
      analyzer.py                 # Go analyzer bridge and schema alias upgrades
      classify.py                 # Endpoint classification and cross-checking
      config.py                   # Repo settings, sheet columns, constants
      models.py                   # Endpoint records and path helpers
      openapi.py                  # OpenAPI parsing and completeness checks
      routes.py                   # Route parsing helpers and commented route scans
      sheets.py                   # Google Sheets authentication and updates
      summary.py                  # Terminal summary output
```

## Prerequisites

- Go, used by `scripts/analyze_handlers/main.go`
- Python 3.9+
- Access to the target repositories you want to audit:
  - `meshery/meshery`
  - optionally `meshery-cloud`
- A bundled OpenAPI file, usually `merged_openapi.yml`

Install Python dependencies in a virtual environment:

```bash
cd meshery/api-audit
python3 -m venv .venv
source .venv/bin/activate
pip install pyyaml gspread google-auth
```

If you only want terminal output, `pyyaml` is the required dependency. `gspread` and `google-auth` are only needed when syncing to Google Sheets.

## Run locally

There are no Make targets required for this version. Run the script directly:

```bash
cd meshery/api-audit
source .venv/bin/activate

python scripts/api-audit.py \
  --meshery-repo /path/to/meshery \
  --spec /path/to/merged_openapi.yml \
  --dry-run
```

Audit both Meshery server and Meshery Cloud:

```bash
python scripts/api-audit.py \
  --meshery-repo /path/to/meshery \
  --cloud-repo /path/to/meshery-cloud \
  --spec /path/to/merged_openapi.yml \
  --dry-run
```

Print per-endpoint details:

```bash
python scripts/api-audit.py \
  --meshery-repo /path/to/meshery \
  --cloud-repo /path/to/meshery-cloud \
  --spec /path/to/merged_openapi.yml \
  --dry-run \
  --verbose
```

## Google Sheet sync

The audit can update the verification spreadsheet when a Sheet ID and Google service account credentials are provided.

Current sheet used for verification:

```text
https://docs.google.com/spreadsheets/d/1YYpMp0H1vlcdnWReaNfVzPcBHXAIlNh52tYBBaRBP7s/edit?usp=sharing
```

Worksheet:

```text
Sheet 4: Verification of API Endpoints - Combined
```

Run a sheet update:

```bash
export SHEET_ID="1YYpMp0H1vlcdnWReaNfVzPcBHXAIlNh52tYBBaRBP7s"
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"

python scripts/api-audit.py \
  --meshery-repo /path/to/meshery \
  --cloud-repo /path/to/meshery-cloud \
  --spec /path/to/merged_openapi.yml \
  --sheet-id "$SHEET_ID"
```

You can also provide credentials inline:

```bash
export GOOGLE_CREDENTIALS_JSON='{"type":"service_account", "...":"..."}'
```

## Supported environment variables

| Variable | Required | Description |
|---|---|---|
| `MESHERY_REPO` | At least one repo path is required | Path to the `meshery/meshery` repository root |
| `CLOUD_REPO` | At least one repo path is required | Path to the `meshery-cloud` repository root |
| `OPENAPI_SPEC_PATH` | Optional if `--spec` is passed | Path to the bundled OpenAPI spec |
| `SHEET_ID` | Required for sheet sync | Google Sheet ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | Required for sheet sync unless using inline credentials | Path to a Google service account JSON file |
| `GOOGLE_CREDENTIALS_JSON` | Alternative for sheet sync | Inline Google service account JSON |

Example using environment variables:

```bash
export MESHERY_REPO="/path/to/meshery"
export CLOUD_REPO="/path/to/meshery-cloud"
export OPENAPI_SPEC_PATH="/path/to/merged_openapi.yml"

python scripts/api-audit.py --dry-run --verbose
```

## How it works

1. The Python entry point loads the bundled OpenAPI spec.
2. The Go helper parses router and handler files with Go AST.
3. The pipeline normalizes routes from Meshery server and Meshery Cloud.
4. It compares router endpoints against OpenAPI paths and methods.
5. It checks whether handlers use `meshery/schemas` types directly or through aliases.
6. It classifies endpoint coverage, schema backing, schema completeness, and schema-driven status.
7. It prints a summary and optionally writes updates to Google Sheets.

This approach makes the audit repeatable, reviewable, and suitable for tracking API schema migration progress over time.
