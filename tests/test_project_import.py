import json

import pytest

from product_finder import db, project_import
from product_finder.config import AppConfig


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.db_path)
    yield c
    c.close()


MINIMAL_YAML = """
schema: product-finder/import/v1
schema_version: 1
project:
  name: Widgets
  create: true
items:
  - name: Blue Widget
    search_terms: [blue widget]
    max_price: 50
    normal_price: 70
    target_price: 40
"""


# --- Parsing -----------------------------------------------------------------


def test_malformed_yaml_reports_document_error(conn, cfg):
    bad = "project:\n  name: Widgets\n  create: true\n  items: [\n"  # unclosed bracket
    plan = project_import.build_plan(conn, cfg, bad)
    assert not plan.valid
    assert any(e.scope == "document" for e in plan.errors)


def test_malformed_json_reports_document_error(conn, cfg):
    bad = '{"project": {"name": "Widgets"}, "items": [{"name": "X"}'  # unbalanced brackets
    plan = project_import.build_plan(conn, cfg, bad)
    assert not plan.valid
    assert any(e.scope == "document" for e in plan.errors)


def test_empty_document_is_a_parse_error(conn, cfg):
    plan = project_import.build_plan(conn, cfg, "   \n")
    assert not plan.valid
    assert "empty" in plan.errors[0].message


def test_non_mapping_document_is_rejected(conn, cfg):
    plan = project_import.build_plan(conn, cfg, "- just\n- a\n- list\n")
    assert not plan.valid
    assert "mapping" in plan.errors[0].message


# --- Validation ----------------------------------------------------------------


def test_item_missing_name_reports_index(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    items:
      - search_terms: [widget]
      - name: Second Widget
        search_terms: [widget2]
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    err = next(e for e in plan.errors if "name" in e.message.lower())
    assert err.index == 0
    assert err.name is None


def test_item_missing_search_terms_reports_index_and_name(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    items:
      - name: Blue Widget
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    err = next(e for e in plan.errors if "search_terms" in e.message)
    assert err.index == 0
    assert err.name == "Blue Widget"


def test_invalid_price_reports_structured_error(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    items:
      - name: Blue Widget
        search_terms: [widget]
        max_price: not-a-number
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    err = next(e for e in plan.errors if "max_price" in e.message)
    assert err.index == 0
    assert err.name == "Blue Widget"


def test_negative_price_rejected(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    items:
      - name: Blue Widget
        search_terms: [widget]
        normal_price: -5
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    assert any("negative" in e.message for e in plan.errors)


def test_unknown_default_source_rejected(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    defaults:
      sources: {not_a_real_source: true}
    items:
      - name: Blue Widget
        search_terms: [widget]
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    err = next(e for e in plan.errors if e.scope == "defaults")
    assert "not_a_real_source" in err.message


def test_unknown_item_source_rejected_with_item_context(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    items:
      - name: Blue Widget
        search_terms: [widget]
        sources: {bogus_marketplace: true}
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    err = next(e for e in plan.errors if e.scope == "item" and "bogus_marketplace" in e.message)
    assert err.index == 0
    assert err.name == "Blue Widget"


def test_invalid_priority_rejected(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    items:
      - name: Blue Widget
        search_terms: [widget]
        priority: urgent
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    assert any("priority" in e.message for e in plan.errors)


def test_project_not_found_without_create_flag(conn, cfg):
    doc = """
    project: {name: Nonexistent Project}
    items:
      - name: Blue Widget
        search_terms: [widget]
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    err = next(e for e in plan.errors if e.scope == "project")
    assert "create: true" in err.message


def test_unsupported_schema_version_rejected(conn, cfg):
    doc = """
    schema_version: 99
    project: {name: Widgets, create: true}
    items:
      - name: Blue Widget
        search_terms: [widget]
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    assert any("schema_version" in e.message for e in plan.errors)


def test_empty_items_list_rejected(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    items: []
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    assert any("non-empty" in e.message for e in plan.errors)


# --- Dry run -------------------------------------------------------------------


def test_dry_run_does_not_write(conn, cfg):
    plan = project_import.build_plan(conn, cfg, MINIMAL_YAML, dry_run_override=True)
    assert plan.valid
    assert plan.dry_run
    result = project_import.apply_plan(conn, plan)
    assert result.dry_run
    assert result.created_items == ["Blue Widget"]
    assert db.list_projects(conn) == []


def test_dry_run_from_document_option(conn, cfg):
    doc = MINIMAL_YAML.replace("schema_version: 1", "schema_version: 1\noptions: {dry_run: true}")
    plan = project_import.build_plan(conn, cfg, doc)
    assert plan.dry_run
    project_import.apply_plan(conn, plan)
    assert db.list_projects(conn) == []


# --- Create ----------------------------------------------------------------


def test_create_new_project_and_items(conn, cfg):
    plan = project_import.build_plan(conn, cfg, MINIMAL_YAML)
    assert plan.valid
    assert plan.project_action == "create"
    result = project_import.apply_plan(conn, plan)
    assert not result.dry_run
    assert result.created_items == ["Blue Widget"]

    projects = db.list_projects(conn)
    assert len(projects) == 1
    assert projects[0]["name"] == "Widgets"
    items = db.list_items(conn, project_id=projects[0]["id"])
    assert len(items) == 1
    assert items[0]["name"] == "Blue Widget"
    assert items[0]["max_price"] == 50
    assert items[0]["normal_price"] == 70
    assert items[0]["target_deal_price"] == 40


def test_create_applies_defaults_and_per_item_override(conn, cfg):
    # A fourth known source (beyond the three builtins) so "ebay + gumtree +
    # facebook" is still a real restriction rather than collapsing to "no
    # restriction" — see build_plan's all-known-sources-enabled shortcut.
    from product_finder.config import ExtraSourceConfig, SourcesConfig

    cfg = AppConfig(
        db_path=cfg.db_path,
        sources=SourcesConfig(extra=[ExtraSourceConfig(name="vinted", type="links", url="https://x/{term}")]),
    )
    doc = """
    project: {name: AI Server, create: true}
    defaults:
      priority: normal
      exclude_terms: [broken, spares]
      sources: {ebay: true, gumtree: true}
    items:
      - name: RTX 3090
        search_terms: ["3090"]
        exclude_terms: [egpu]
      - name: RTX 4090
        search_terms: ["4090"]
        priority: high
        sources: {facebook: true}
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert plan.valid, plan.errors
    result = project_import.apply_plan(conn, plan)
    assert set(result.created_items) == {"RTX 3090", "RTX 4090"}

    project_id = db.list_projects(conn)[0]["id"]
    items = {row["name"]: row for row in db.list_items(conn, project_id=project_id)}

    gpu3090 = db._item_from_row(items["RTX 3090"])
    assert gpu3090.priority == "normal"
    assert set(gpu3090.exclude_terms) == {"broken", "spares", "egpu"}
    assert set(gpu3090.sources) == {"ebay", "gumtree"}

    gpu4090 = db._item_from_row(items["RTX 4090"])
    assert gpu4090.priority == "high"
    # item's own sources dict is merged on top of defaults, not a full replace
    assert set(gpu4090.sources) == {"ebay", "gumtree", "facebook"}


def test_merge_defaults_false_ignores_defaults(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    options: {merge_defaults: false}
    defaults:
      exclude_terms: [broken]
      priority: high
    items:
      - name: Blue Widget
        search_terms: [widget]
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert plan.valid
    item_plan = plan.items[0]
    assert item_plan.item_cfg.priority == "normal"  # falls back to field default, not defaults block
    assert item_plan.item_cfg.exclude_terms == []


def test_item_enabled_false_archives_on_create(conn, cfg):
    doc = """
    project: {name: Widgets, create: true}
    items:
      - name: Blue Widget
        search_terms: [widget]
        enabled: false
    """
    plan = project_import.build_plan(conn, cfg, doc)
    project_import.apply_plan(conn, plan)
    project_id = db.list_projects(conn)[0]["id"]
    item = db.list_items(conn, project_id=project_id)[0]
    assert bool(item["archived"]) is True


# --- Update / upsert ---------------------------------------------------------


def test_upsert_updates_existing_item_by_name(conn, cfg):
    project_id = db.create_project(conn, "Widgets")
    from product_finder.config import ItemConfig

    db.create_item(
        conn, project_id,
        ItemConfig(name="Blue Widget", terms=["old term"], max_price=10, priority="low"),
    )

    doc = f"""
    project: {{id: {project_id}}}
    items:
      - name: Blue Widget
        search_terms: [new term]
        max_price: 99
        priority: high
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert plan.valid
    assert plan.project_action == "update"
    item_plan = plan.items[0]
    assert item_plan.action == "update"
    assert item_plan.before["max_price"] == 10

    result = project_import.apply_plan(conn, plan)
    assert result.updated_items == ["Blue Widget"]
    assert result.created_items == []

    items = db.list_items(conn, project_id=project_id)
    assert len(items) == 1  # upserted in place, not duplicated
    assert items[0]["max_price"] == 99
    assert items[0]["priority"] == "high"
    assert json.loads(items[0]["terms"]) == ["new term"]


def test_upsert_matches_existing_item_regardless_of_case(conn, cfg):
    # Regression: importing "NVIDIA RTX 3080 Ti" against an existing
    # "NVidia RTX 3080 Ti" item used to create a second, near-duplicate item
    # instead of updating the one already there.
    project_id = db.create_project(conn, "AI Server")
    from product_finder.config import ItemConfig

    db.create_item(
        conn, project_id,
        ItemConfig(name="NVidia RTX 3080 Ti", terms=["3080 ti"], max_price=550, notes="Prefer EVGA."),
    )

    doc = f"""
    project: {{id: {project_id}}}
    items:
      - name: NVIDIA RTX 3080 Ti
        search_terms: ["3080 ti", "rtx 3080 ti"]
        max_price: 550
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert plan.valid, plan.errors
    assert plan.items[0].action == "update"

    result = project_import.apply_plan(conn, plan)
    assert result.updated_items == ["NVIDIA RTX 3080 Ti"]
    assert result.created_items == []
    assert len(db.list_items(conn, project_id=project_id)) == 1


def test_duplicate_item_names_in_same_document_rejected_case_insensitively(conn, cfg):
    doc = """
    project: {name: AI Server, create: true}
    items:
      - name: NVIDIA RTX 3080 Ti
        search_terms: [3080 ti]
      - name: NVidia RTX 3080 Ti
        search_terms: [3080ti]
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    err = next(e for e in plan.errors if "Duplicate item name" in e.message)
    assert err.index == 1


def test_project_resolved_by_name_case_insensitive(conn, cfg):
    db.create_project(conn, "Widgets")
    doc = """
    project: {name: WIDGETS}
    items:
      - name: Blue Widget
        search_terms: [widget]
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert plan.valid
    assert plan.project_action == "update"


def test_project_id_not_found(conn, cfg):
    doc = """
    project: {id: 999}
    items:
      - name: Blue Widget
        search_terms: [widget]
    """
    plan = project_import.build_plan(conn, cfg, doc)
    assert not plan.valid
    assert any("No project with id 999" in e.message for e in plan.errors)


# --- Export --------------------------------------------------------------------


def test_export_round_trips_through_import(conn, cfg):
    plan = project_import.build_plan(conn, cfg, MINIMAL_YAML)
    project_import.apply_plan(conn, plan)
    project_id = db.list_projects(conn)[0]["id"]

    doc = project_import.export_project(conn, project_id)
    assert doc["schema"] == project_import.SCHEMA_ID
    assert doc["project"]["name"] == "Widgets"
    assert doc["items"][0]["name"] == "Blue Widget"
    assert doc["items"][0]["max_price"] == 50

    yaml_text = project_import.to_yaml(doc)
    json_text = project_import.to_json(doc)
    assert json.loads(json_text)["project"]["name"] == "Widgets"

    # Re-importing the export is a no-op update (upsert by name, same values)
    reimport_plan = project_import.build_plan(conn, cfg, yaml_text)
    assert reimport_plan.valid
    assert reimport_plan.project_action == "update"
    assert reimport_plan.items[0].action == "update"
    result = project_import.apply_plan(conn, reimport_plan)
    assert result.updated_items == ["Blue Widget"]
    assert len(db.list_items(conn, project_id=project_id)) == 1


def test_export_omits_archived_items(conn, cfg):
    project_id = db.create_project(conn, "Widgets")
    from product_finder.config import ItemConfig

    item_id = db.create_item(conn, project_id, ItemConfig(name="Old Widget", terms=["old"]))
    db.set_item_archived(conn, item_id, True)

    doc = project_import.export_project(conn, project_id)
    assert doc["items"] == []
