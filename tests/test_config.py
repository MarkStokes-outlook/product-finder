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
    assert cfg.alerts.markdown_report is True
    assert len(cfg.projects) == 1
    project = cfg.projects[0]
    assert project.slug == "coachhouse-tools"
    assert len(project.items) == 2
    saw = project.items[0]
    assert saw.name == "Track Saw"
    assert saw.max_price == 400
    assert saw.normal_price == 500
    assert saw.target_deal_price == 300
    assert saw.priority == "high"
    assert "toy" in saw.exclude_terms
    assert saw.sources is None  # all enabled sources


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
    assert cfg.projects[0].items[0].priority == "normal"
    assert cfg.sources.enabled_names() == ["ebay", "gumtree", "facebook"]
