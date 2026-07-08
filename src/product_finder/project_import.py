"""Project/item import & export — JSON/YAML backup and bulk-load format.

Schema (`product-finder/import/v1`): a document names a project (by id, for
an existing project, or by name — optionally creating it), an optional
`defaults` block, and a list of `items`. Each item can override any default.
See docs/imports/*.example.{yaml,json} for worked examples.

Two-phase by design: `build_plan()` only ever reads the database and never
writes, producing an `ImportPlan` the caller can render as a preview
(including a full validation error list) before anything is committed.
`apply_plan()` performs the writes and is only meant to be called with a
plan whose `valid` flag is True — callers MUST re-validate (call
build_plan again on the same raw text) at the point of commit rather than
trusting a plan handed back from an earlier request/render, since the
database may have changed in between (e.g. the target project renamed).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import yaml

from . import db
from .config import AppConfig, ItemConfig

SCHEMA_ID = "product-finder/import/v1"
SCHEMA_VERSION = 1

_PRIORITIES = ("high", "normal", "low")


class ImportParseError(Exception):
    """Raised when the raw text isn't parseable as YAML or JSON."""


@dataclass
class ImportIssue:
    """A single structured validation error.

    `index`/`name` are set for item-scoped issues so a caller can point the
    user at exactly which row of `items` is wrong, even before it has a
    database id (it might not exist yet).
    """

    scope: str  # "document" | "project" | "options" | "defaults" | "item"
    message: str
    index: int | None = None
    name: str | None = None

    def __str__(self) -> str:  # convenient for flash messages / assertions
        if self.index is not None:
            label = f"item #{self.index + 1}"
            if self.name:
                label += f" '{self.name}'"
            return f"{label}: {self.message}"
        if self.scope != "document":
            return f"{self.scope}: {self.message}"
        return self.message


@dataclass
class ItemPlan:
    index: int
    name: str
    action: str  # "create" | "update"
    existing_id: int | None
    item_cfg: ItemConfig
    enabled: bool
    before: dict | None  # None when action == "create"
    after: dict


@dataclass
class ImportPlan:
    valid: bool
    errors: list[ImportIssue]
    dry_run: bool
    project_action: str | None  # "create" | "update" | None (unresolved — see errors)
    project_id: int | None
    project_name: str
    items: list[ItemPlan] = field(default_factory=list)


@dataclass
class ImportResult:
    dry_run: bool
    project_action: str
    project_id: int
    project_name: str
    created_items: list[str]
    updated_items: list[str]


def parse_document(raw_text: str) -> dict:
    """Parse pasted/uploaded text as YAML (a superset of JSON), raising a
    single clear error for either malformed YAML or malformed JSON."""
    if not raw_text or not raw_text.strip():
        raise ImportParseError("Import file is empty.")
    try:
        doc = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ImportParseError(f"Could not parse as YAML or JSON{location}: {exc}") from exc
    if doc is None:
        raise ImportParseError("Import file has no content.")
    if not isinstance(doc, dict):
        raise ImportParseError("Import document must be a mapping/object at the top level.")
    return doc


def _item_dict(item_cfg: ItemConfig, enabled: bool) -> dict:
    return {
        "name": item_cfg.name,
        "priority": item_cfg.priority,
        "search_terms": item_cfg.terms,
        "exclude_terms": item_cfg.exclude_terms,
        "max_price": item_cfg.max_price,
        "normal_price": item_cfg.normal_price,
        "target_price": item_cfg.target_deal_price,
        "notes": item_cfg.notes,
        "sources": item_cfg.sources,
        "enabled": enabled,
    }


def _find_project_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    target = name.strip().lower()
    for row in db.list_projects(conn, include_archived=True):
        if row["name"].strip().lower() == target:
            return row
    return None


def _find_item_by_name(conn: sqlite3.Connection, project_id: int, name: str) -> sqlite3.Row | None:
    # Case/whitespace-insensitive, matching _find_project_by_name: an item
    # name is a human label, not an identifier, so "NVIDIA RTX 3080 Ti" and
    # "NVidia RTX 3080 Ti" should upsert onto the same row rather than
    # silently creating a near-duplicate the next time a project's canonical
    # spelling doesn't match whatever was typed in first. (The DB's own
    # (project_id, name) UNIQUE constraint is still case-sensitive — it just
    # means this lookup, not the schema, is what prevents the duplicate.)
    target = name.strip().lower()
    for row in db.list_items(conn, project_id=project_id, include_archived=True):
        if row["name"].strip().lower() == target:
            return row
    return None


def _as_float(errors: list[ImportIssue], scope: str, index: int | None, name: str | None,
              value, field_name: str) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        errors.append(ImportIssue(scope, f"'{field_name}' must be a number, got {value!r}.", index, name))
        return None
    if parsed < 0:
        errors.append(ImportIssue(scope, f"'{field_name}' cannot be negative.", index, name))
    return parsed


def build_plan(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    raw_text: str,
    dry_run_override: bool | None = None,
) -> ImportPlan:
    """Validate `raw_text` and resolve it into a plan. Read-only — never
    writes to the database, so it's safe to call repeatedly for a preview
    and again just before commit."""
    errors: list[ImportIssue] = []
    try:
        doc = parse_document(raw_text)
    except ImportParseError as exc:
        return ImportPlan(
            valid=False,
            errors=[ImportIssue("document", str(exc))],
            dry_run=bool(dry_run_override),
            project_action=None,
            project_id=None,
            project_name="",
        )

    schema = doc.get("schema")
    if schema is not None and not str(schema).startswith("product-finder/import/"):
        errors.append(ImportIssue("document", f"Unsupported schema '{schema}' (expected '{SCHEMA_ID}')."))

    schema_version_raw = doc.get("schema_version", SCHEMA_VERSION)
    try:
        schema_version = int(schema_version_raw)
        if schema_version != SCHEMA_VERSION:
            errors.append(ImportIssue(
                "document",
                f"Unsupported schema_version {schema_version_raw!r} (expected {SCHEMA_VERSION}).",
            ))
    except (TypeError, ValueError):
        errors.append(ImportIssue("document", f"schema_version must be an integer, got {schema_version_raw!r}."))

    project_raw = doc.get("project")
    if project_raw is None:
        project_raw = {}
    elif not isinstance(project_raw, dict):
        errors.append(ImportIssue("project", "'project' must be a mapping."))
        project_raw = {}

    options_raw = doc.get("options")
    if options_raw is None:
        options_raw = {}
    elif not isinstance(options_raw, dict):
        errors.append(ImportIssue("options", "'options' must be a mapping."))
        options_raw = {}

    defaults_raw = doc.get("defaults")
    if defaults_raw is None:
        defaults_raw = {}
    elif not isinstance(defaults_raw, dict):
        errors.append(ImportIssue("defaults", "'defaults' must be a mapping."))
        defaults_raw = {}

    items_raw = doc.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        errors.append(ImportIssue("document", "'items' must be a non-empty list."))
        items_raw = []

    # --- options ---------------------------------------------------------
    doc_dry_run = bool(options_raw.get("dry_run", False))
    dry_run = doc_dry_run if dry_run_override is None else bool(dry_run_override)

    upsert_mode = str(options_raw.get("upsert_mode", "name"))
    if upsert_mode != "name":
        errors.append(ImportIssue(
            "options", f"Unsupported upsert_mode '{upsert_mode}' (only 'name' is supported)."
        ))

    merge_defaults = bool(options_raw.get("merge_defaults", True))

    # --- known-source validation ------------------------------------------
    allowed_sources = set(cfg.sources.all_names())

    # --- defaults ----------------------------------------------------------
    default_priority = str(defaults_raw.get("priority", "normal")).lower()
    if default_priority not in _PRIORITIES:
        errors.append(ImportIssue("defaults", f"priority must be one of {_PRIORITIES}, got '{default_priority}'."))
        default_priority = "normal"

    default_exclude_terms = [str(t) for t in (defaults_raw.get("exclude_terms") or [])]

    default_sources_raw = defaults_raw.get("sources")
    if default_sources_raw is None:
        default_sources_raw = {}
    elif not isinstance(default_sources_raw, dict):
        errors.append(ImportIssue("defaults", "'sources' must be a mapping of source name to true/false."))
        default_sources_raw = {}
    unknown_default_sources = sorted(k for k in default_sources_raw if k not in allowed_sources)
    if unknown_default_sources:
        errors.append(ImportIssue("defaults", f"Unknown source(s): {', '.join(unknown_default_sources)}"))

    default_enabled = bool(defaults_raw.get("enabled", True))

    # --- project resolution --------------------------------------------
    project_id_raw = project_raw.get("id")
    project_name = str(project_raw.get("name") or "").strip()
    project_create = bool(project_raw.get("create", False))

    existing_project = None
    if project_id_raw is not None:
        try:
            pid = int(project_id_raw)
        except (TypeError, ValueError):
            errors.append(ImportIssue("project", f"project.id must be an integer, got {project_id_raw!r}."))
            pid = None
        if pid is not None:
            existing_project = db.get_project(conn, pid)
            if existing_project is None:
                errors.append(ImportIssue("project", f"No project with id {pid}."))
    elif project_name:
        existing_project = _find_project_by_name(conn, project_name)
    else:
        errors.append(ImportIssue("project", "project.name or project.id is required."))

    project_action: str | None
    resolved_project_id: int | None
    resolved_project_name: str
    if existing_project is not None:
        project_action = "update"
        resolved_project_id = existing_project["id"]
        resolved_project_name = existing_project["name"]
    elif project_name and project_create:
        project_action = "create"
        resolved_project_id = None
        resolved_project_name = project_name
    elif project_name and project_id_raw is None:
        errors.append(ImportIssue(
            "project",
            f"Project '{project_name}' does not exist. Set project.create: true to create it.",
        ))
        project_action = None
        resolved_project_id = None
        resolved_project_name = project_name
    else:
        project_action = None
        resolved_project_id = None
        resolved_project_name = project_name

    # --- items -------------------------------------------------------------
    item_plans: list[ItemPlan] = []
    seen_names: dict[str, int] = {}
    for index, raw_item in enumerate(items_raw):
        if not isinstance(raw_item, dict):
            errors.append(ImportIssue("item", "Item must be a mapping.", index=index))
            continue

        name = str(raw_item.get("name") or "").strip()
        if not name:
            errors.append(ImportIssue("item", "Item is missing 'name'.", index=index))
            continue
        # Case/whitespace-insensitive, matching _find_item_by_name below — two
        # items in the same document differing only by case (e.g. "NVIDIA
        # RTX 3080 Ti" vs "NVidia RTX 3080 Ti") would otherwise both upsert
        # onto the *same* existing row, silently discarding whichever one
        # applied second, rather than being flagged for the user to fix.
        name_key = name.lower()
        if name_key in seen_names:
            errors.append(ImportIssue(
                "item", f"Duplicate item name '{name}' (also item #{seen_names[name_key] + 1}).",
                index=index, name=name,
            ))
        seen_names[name_key] = index

        def field_value(key: str, default=None):
            if key in raw_item:
                return raw_item[key]
            if merge_defaults and key in defaults_raw:
                return defaults_raw[key]
            return default

        priority = str(field_value("priority", "normal")).lower()
        if priority not in _PRIORITIES:
            errors.append(ImportIssue(
                "item", f"priority must be one of {_PRIORITIES}, got '{priority}'.", index=index, name=name,
            ))
            priority = "normal"

        search_terms_raw = raw_item.get("search_terms")
        if not isinstance(search_terms_raw, list) or not search_terms_raw:
            errors.append(ImportIssue(
                "item", "requires a non-empty 'search_terms' list.", index=index, name=name,
            ))
            search_terms = []
        else:
            search_terms = [str(t).strip() for t in search_terms_raw if str(t).strip()]
            if not search_terms:
                errors.append(ImportIssue(
                    "item", "requires a non-empty 'search_terms' list.", index=index, name=name,
                ))

        item_exclude_raw = raw_item.get("exclude_terms") or []
        if not isinstance(item_exclude_raw, list):
            errors.append(ImportIssue("item", "'exclude_terms' must be a list.", index=index, name=name))
            item_exclude_raw = []
        merged_exclude = (default_exclude_terms if merge_defaults else []) + [str(t) for t in item_exclude_raw]
        exclude_terms = list(dict.fromkeys(merged_exclude))  # de-dupe, keep first-seen order

        max_price = _as_float(errors, "item", index, name, field_value("max_price"), "max_price")
        normal_price = _as_float(errors, "item", index, name, field_value("normal_price"), "normal_price")
        target_price = _as_float(errors, "item", index, name, field_value("target_price"), "target_price")

        notes = str(field_value("notes", "") or "")
        enabled = bool(field_value("enabled", default_enabled))

        item_sources_raw = raw_item.get("sources")
        if item_sources_raw is None:
            item_sources_raw = {}
        elif not isinstance(item_sources_raw, dict):
            errors.append(ImportIssue(
                "item", "'sources' must be a mapping of source name to true/false.", index=index, name=name,
            ))
            item_sources_raw = {}
        unknown_item_sources = sorted(k for k in item_sources_raw if k not in allowed_sources)
        if unknown_item_sources:
            errors.append(ImportIssue(
                "item", f"Unknown source(s): {', '.join(unknown_item_sources)}", index=index, name=name,
            ))

        merged_sources_map = dict(default_sources_raw) if merge_defaults else {}
        merged_sources_map.update(item_sources_raw)
        enabled_source_names = sorted(n for n, v in merged_sources_map.items() if v)
        if not merged_sources_map or set(enabled_source_names) >= allowed_sources:
            sources: list[str] | None = None  # no restriction
        else:
            sources = enabled_source_names

        item_cfg = ItemConfig(
            name=name,
            terms=search_terms,
            max_price=max_price,
            normal_price=normal_price,
            target_deal_price=target_price,
            priority=priority,
            notes=notes,
            exclude_terms=exclude_terms,
            sources=sources,
        )

        existing_item = (
            _find_item_by_name(conn, resolved_project_id, name)
            if project_action == "update" and resolved_project_id is not None
            else None
        )
        before = None
        if existing_item is not None:
            before = _item_dict(db._item_from_row(existing_item), not bool(existing_item["archived"]))

        item_plans.append(ItemPlan(
            index=index,
            name=name,
            action="update" if existing_item is not None else "create",
            existing_id=existing_item["id"] if existing_item is not None else None,
            item_cfg=item_cfg,
            enabled=enabled,
            before=before,
            after=_item_dict(item_cfg, enabled),
        ))

    return ImportPlan(
        valid=not errors,
        errors=errors,
        dry_run=dry_run,
        project_action=project_action,
        project_id=resolved_project_id,
        project_name=resolved_project_name,
        items=item_plans,
    )


def apply_plan(conn: sqlite3.Connection, plan: ImportPlan) -> ImportResult:
    """Write a valid plan to the database. Raises ValueError if the plan
    failed validation — callers must check `plan.valid` (or just re-run
    build_plan and inspect it) before calling this."""
    if not plan.valid:
        raise ValueError("Cannot apply an import plan that failed validation.")

    created_items: list[str] = []
    updated_items: list[str] = []

    if plan.dry_run:
        for item_plan in plan.items:
            (created_items if item_plan.action == "create" else updated_items).append(item_plan.name)
        return ImportResult(
            dry_run=True,
            project_action=plan.project_action,
            project_id=plan.project_id,
            project_name=plan.project_name,
            created_items=created_items,
            updated_items=updated_items,
        )

    project_id = plan.project_id
    if plan.project_action == "create":
        project_id = db.create_project(conn, plan.project_name)

    for item_plan in plan.items:
        if item_plan.action == "create":
            item_id = db.create_item(conn, project_id, item_plan.item_cfg)
            created_items.append(item_plan.name)
        else:
            db.update_item(conn, item_plan.existing_id, item_plan.item_cfg)
            item_id = item_plan.existing_id
            updated_items.append(item_plan.name)
        db.set_item_archived(conn, item_id, not item_plan.enabled)

    return ImportResult(
        dry_run=False,
        project_action=plan.project_action,
        project_id=project_id,
        project_name=plan.project_name,
        created_items=created_items,
        updated_items=updated_items,
    )


# --- Export: the reverse direction, same schema -----------------------------


def export_project(conn: sqlite3.Connection, project_id: int) -> dict:
    """Serialise a project's active items into the same v1 import schema, so
    the result can be re-imported as-is (backup/share/round-trip). Archived
    items are left out, same as everywhere else active state is listed."""
    project = db.get_project(conn, project_id)
    if project is None:
        raise ValueError(f"No project with id {project_id}")

    doc_items = []
    for row in db.list_items(conn, project_id=project_id, include_archived=False):
        item_cfg = db._item_from_row(row)
        entry: dict = {"name": item_cfg.name, "search_terms": item_cfg.terms}
        if item_cfg.exclude_terms:
            entry["exclude_terms"] = item_cfg.exclude_terms
        if item_cfg.max_price is not None:
            entry["max_price"] = item_cfg.max_price
        if item_cfg.normal_price is not None:
            entry["normal_price"] = item_cfg.normal_price
        if item_cfg.target_deal_price is not None:
            entry["target_price"] = item_cfg.target_deal_price
        if item_cfg.priority != "normal":
            entry["priority"] = item_cfg.priority
        if item_cfg.notes:
            entry["notes"] = item_cfg.notes
        if item_cfg.sources is not None:
            entry["sources"] = {name: True for name in item_cfg.sources}
        doc_items.append(entry)

    project_block = {"name": project["name"], "slug": project["slug"], "create": True}
    if project["sources"]:
        project_block["sources"] = json.loads(project["sources"])

    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "project": project_block,
        "options": {"dry_run": False, "upsert_mode": "name", "merge_defaults": True},
        "items": doc_items,
    }


def to_yaml(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def to_json(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"
