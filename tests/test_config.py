from pathlib import Path

import pytest

from product_finder.config import ConfigError, load_config

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def test_load_example_config():
    cfg = load_config(EXAMPLE)
    assert cfg.postcode == "BL0"
    assert cfg.radius_miles == 30
    assert cfg.interval_minutes == 60
    assert cfg.alerts.console is True
    by_slug = {p.slug: p for p in cfg.projects}
    assert "coachhouse-tools" in by_slug

    project = by_slug["coachhouse-tools"]
    assert set(project.sources) == {"ebay", "gumtree", "facebook", "johnpyeauctions", "preloved"}
    assert "cexwebuy" not in project.sources  # project-level restriction
    assert len(project.items) == 2
    saw = project.items[0]
    assert saw.name == "Track Saw"
    assert saw.max_price == 400
    assert saw.normal_price == 500
    assert saw.target_deal_price == 300
    assert saw.priority == "high"
    assert "toy" in saw.exclude_terms
    assert saw.sources is None  # all enabled sources

    # Demo projects exercising the config-driven extra sources — each scopes
    # `sources:` to only the endpoints that actually suit its domain.
    gaming = by_slug["gaming-pc-upgrade"]
    gpu = gaming.items[0]
    assert gpu.name == "Graphics Card"
    assert set(gpu.sources) >= {"hardwareswapuk", "cexwebuy"}

    workshop = by_slug["workshop-garden-kit"]
    assert set(workshop.items[0].sources) >= {"johnpyeauctions", "preloved"}

    home_office = by_slug["home-office-refresh"]
    assert set(home_office.items[0].sources) >= {"vinted", "preloved"}


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/config.yaml")


def test_item_requires_terms(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "projects:\n"
        "  - name: P\n"
        "    items:\n"
        "      - name: Widget\n"
    )
    with pytest.raises(ConfigError, match="no search terms"):
        load_config(bad)


def test_invalid_priority(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "projects:\n"
        "  - name: P\n"
        "    items:\n"
        "      - name: Widget\n"
        "        terms: [widget]\n"
        "        priority: urgent\n"
    )
    with pytest.raises(ConfigError, match="priority"):
        load_config(bad)


def test_defaults_applied(tmp_path):
    minimal = tmp_path / "minimal.yaml"
    minimal.write_text(
        "projects:\n"
        "  - name: Homelab\n"
        "    items:\n"
        "      - name: Switch\n"
        "        terms: [managed switch]\n"
    )
    cfg = load_config(minimal)
    assert cfg.projects[0].slug == "homelab"
    assert cfg.projects[0].sources is None  # no project-level restriction
    assert cfg.projects[0].items[0].priority == "normal"
    assert cfg.sources.enabled_names() == ["ebay", "gumtree", "facebook"]


def test_project_sources_restrict_search(tmp_path):
    cfg_file = tmp_path / "p.yaml"
    cfg_file.write_text(
        "projects:\n"
        "  - name: Power Tools\n"
        "    sources: [ebay, gumtree]\n"
        "    items:\n"
        "      - name: Drill\n"
        "        terms: [drill]\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.projects[0].sources == ["ebay", "gumtree"]


def test_project_unknown_source_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "projects:\n"
        "  - name: P\n"
        "    sources: [not-a-real-source]\n"
        "    items:\n"
        "      - name: Widget\n"
        "        terms: [widget]\n"
    )
    with pytest.raises(ConfigError, match="unknown sources"):
        load_config(bad)
