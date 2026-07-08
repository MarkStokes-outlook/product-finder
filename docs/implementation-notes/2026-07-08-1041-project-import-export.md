# Project JSON/YAML import & export — implementation notes

**Date:** 2026-07-08 ~10:41
**Tests:** 453 passing (419 prior + 34 new: 25 in `test_project_import.py`, 9 in `test_web_import.py`)
**Trigger:** Mark asked for a "product import" capability on the Project
screen — paste or upload a JSON/YAML file of items into a project, schema
validation, and (once the shape was agreed via two worked examples he
dropped in `docs/imports/`) preview-before-commit, dry-run, upsert-by-name,
defaults merging, unknown-source rejection, structured per-item errors, and
a matching export so the format doubles as a backup/sharing mechanism.

## What changed

- **`src/product_finder/project_import.py`** (new): the whole feature lives
  here, separate from `db.py`/`config.py` since it's a document-shaped
  input format, not the app's own config loading.
  - `parse_document()` — `yaml.safe_load` handles both YAML and JSON (JSON
    is a YAML subset), so one parser covers both; malformed input of either
    flavour raises `ImportParseError` with a line/column hint when PyYAML
    provides one.
  - `build_plan()` — **read-only**, never writes. Validates the whole
    document (project resolution, defaults, every item) and returns an
    `ImportPlan` with a `valid` flag and a flat `errors: list[ImportIssue]`
    (each carrying `scope`, `index`, `name` so the UI/tests can point at
    exactly which item is wrong). This is the "validate before writing" and
    "preview before commit" requirement in one function — the web layer
    calls it once to render the preview, then calls it **again** at commit
    time against the same raw text rather than trusting the earlier plan,
    since the target project could have changed in between requests.
  - `apply_plan()` — writes a plan that already passed validation. Refuses
    (`ValueError`) to run on an invalid plan. `dry_run` short-circuits
    before any `db.*` write call and reports what *would* have happened
    from the plan alone.
  - Upsert is by exact item name within the resolved project (matches the
    `(project_id, name)` UNIQUE constraint items are actually stored under —
    deliberately not case-insensitive, so it can never accept a name the DB
    itself would reject as a collision, or miss one it would accept as
    distinct). Project resolution *is* case-insensitive on name (existing
    project lookup only — new project creation is untouched), since
    `project.create: true` re-running an import with different casing
    creating a duplicate project felt like the more likely footgun.
  - Defaults merge: scalar fields (`priority`, `notes`, `max_price`, etc.)
    are "item overrides default overrides field default"; `exclude_terms`
    is a de-duplicated union (defaults are generally noise-word lists that
    should apply everywhere, items can only add to them); `sources` is a
    dict merge (item's per-source keys override defaults' per-source keys,
    not a full replace) before being collapsed to `ItemConfig.sources`'
    existing `list[str] | None` shape — `None` means "no restriction",
    same meaning it already has everywhere else in the app. If the merged
    enabled-set covers every source the app knows about, it collapses to
    `None` rather than an explicit list (behaviourally identical, and it's
    what lets a fully-permissive `defaults.sources` block export/re-import
    losslessly).
  - `options.merge_defaults: false` skips the defaults block entirely
    (items must be fully self-contained); `options.upsert_mode` only
    accepts `"name"` today — anything else is a validation error, not a
    silent ignore, so a typo doesn't quietly do the wrong thing.
  - Unknown sources (in `defaults.sources` or any item's `sources`) are
    validated against `cfg.sources.all_names()` — the same set
    `config.py`'s own YAML loader already treats as canonical — and reject
    the whole document (not just that one field), consistent with
    "validate before writing".
  - `enabled` (per item, defaulting from `defaults.enabled`, defaulting to
    `true`) maps onto the existing archived/active toggle
    (`db.set_item_archived`) rather than adding a new column — there was
    already a mechanism for "this item exists but isn't active".
  - Two fields appear in Mark's own example files but have no backing
    column anywhere (`project.description`, per-item `category`) — both
    are accepted and silently ignored rather than rejected as unknown
    fields, so his own examples import cleanly. Only source names are
    validated strictly; arbitrary extra keys are forward-compatible no-ops.
  - `export_project()` — the reverse direction, same schema, active items
    only (archived ones are left out, same convention as everywhere else
    "active" is listed). Round-trips: re-importing an unmodified export is
    a same-values `update` for every item (verified in both
    `test_project_import.py` and `test_web_import.py`).
- **`src/product_finder/web/app.py`**: three new routes —
  `GET/POST /projects/import` (form + preview, file upload takes priority
  over pasted text if both are present), `POST /projects/import/commit`
  (re-validates, then writes unless dry-run), `GET /projects/<id>/export`
  (`?format=yaml|json`, downloads as an attachment named `<slug>.<ext>`).
- **`src/product_finder/web/templates/project_import.html`** (new): single
  template handling all three states — blank form, error list (with a
  pre-filled textarea to fix and re-preview), and a valid preview (item
  table with action/prices/priority/sources/enabled, a hidden `raw_text`
  field carrying the exact document through to commit, dry-run checkbox).
- **`projects.html`**: "Import file…" button next to the existing
  "Import from YAML" (config.yaml merge) action — deliberately kept as a
  separate, differently-labelled button since they're different things
  (one merges `config.yaml` in place, this one is an arbitrary
  document-shaped upload).
- **`project_detail.html`**: "Export YAML" / "Export JSON" buttons in the
  page header actions.

## Validated against Mark's own example files

`docs/imports/ai-server.example.yaml` and `.json` both fail validation
against a bare `AppConfig()` (they use `hardwareswapuk`, `vinted`,
`johnpyeauctions`, `preloved`, `cexwebuy` — all config-defined `extra:`
sources, not built-ins) but both pass, dry-run, and commit cleanly against
the repo's actual `config.yaml`, which defines all of them. Ran a full
smoke test through `create_app()`'s real test client (not just unit tests):
form load → preview → commit → project appears on `/projects` → export in
both formats — all 200s, no tracebacks.

## What's unchanged

`import-config` (the existing "merge `config.yaml` into the DB" button) is
untouched — this is a parallel, independent path for arbitrary
document-shaped imports, not a replacement.
