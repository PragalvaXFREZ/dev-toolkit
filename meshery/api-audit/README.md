# Meshery API Audit

`api_audit.py` audits Meshery Server API endpoints by comparing code and schema sources, then optionally syncs the results to a Google Sheet.

## What It Does

The script compares three sources inside the `meshery/meshery` repo:

1. `server/router/server.go`
   Finds registered routes from the Meshery router.
2. `docs/data/openapi.yml`
   Checks whether an endpoint is codified in the API spec.
3. `server/handlers/*.go`
   Checks whether the handler is schema-driven by looking for actual schema type usage.

For each endpoint, it computes:

- `Category`
- `Sub-Category`
- `Endpoints`
- `Methods`
- `API Endpoint is codifed within a schema defined in meshery/schemas.`
- `Schema-driven in Meshery Server (...)`
- `Notes`
- `Change Log`

## Commented Routes

Commented routes are intentionally included in the audit.

- If an endpoint exists only in commented code, the `Notes` column includes `commented out / legacy`.
- If both an active and commented version of the same endpoint exist, the active version wins.
- The `Notes` column is deterministic and is updated on every run for matched rows.

This makes the sheet useful as an audit/inventory source while still distinguishing inactive routes.

## How Classification Works

### Codified

An endpoint is marked:

- `TRUE` when all of its methods are present in the server-applicable OpenAPI schema
- `Partial` when only some methods are server-applicable, or when schema coverage exists only in cloud-only entries
- `FALSE` when the path is not represented in the spec

### Driven

An endpoint is marked:

- `TRUE` when the handler function uses versioned schema models
- `Partial` when it only uses `models/core`
- `FALSE` when it does not use schema imports in the function body

## Sheet Update Behavior

When syncing to the Google Sheet, the script:

- matches existing rows by normalized endpoint path plus method overlap
- updates `codified`, `driven`, and `Notes` when they change
- updates the `Change Log` date when a row changes
- inserts new rows inside the matching `Category` / `Sub-Category` section when possible
- falls back to appending at the end only when the category does not already exist
- currently writes to worksheet index `4` in the spreadsheet

## Requirements

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Create a `.env` file in this directory:

```bash
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json
SHEET_ID=your_google_sheet_id
MESHERY_REPO_PATH=/absolute/path/to/meshery
```

`api_audit.py` auto-loads `.env`, so users can run the script directly without sourcing it first.

Supported variables:

- `MESHERY_REPO_PATH`
- `SHEET_ID`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `GOOGLE_CREDENTIALS_JSON`

## Usage

### Dry Run

Preview what would change in the sheet without writing:

```bash
python3 api_audit.py --dry-run
```

### Write to the Sheet

Apply the computed updates to the configured sheet:

```bash
python3 api_audit.py
```

### Override Values Explicitly

You can still pass values manually:

```bash
python3 api_audit.py --repo /path/to/meshery --sheet-id "$SHEET_ID" --dry-run
```

### Verbose Output

Print per-endpoint classifications:

```bash
python3 api_audit.py --dry-run --verbose
```

## Typical Workflow

1. Create `.env`
2. Install dependencies
3. Run a dry-run:

```bash
python3 api_audit.py --dry-run
```

4. Review the proposed changes
5. Run the real update:

```bash
python3 api_audit.py
```

## Notes

- The script updates a sheet column named `Notes`; it does not create native Google Sheets comment bubbles.
- The script currently targets worksheet index `4` directly.
- The sheet is useful as an audit artifact, but endpoint-level code edits should still be validated against the Meshery source tree.
