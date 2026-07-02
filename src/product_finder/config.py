"""YAML config loading and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

KNOWN_SOURCES = ("ebay", "gumtree", "facebook")


class ConfigError(Exception):
    pass


@dataclass
class ItemConfig:
    name: str
    terms: list[str]
    max_price: float | None = None
    normal_price: float | None = None
    target_deal_price: float | None = None
    priority: str = "normal"  # high | normal | low
    notes: str = ""
    exclude_terms: list[str] = field(default_factory=list)
    sources: list[str] | None = None  # None = all enabled sources


@dataclass
class ProjectConfig:
    name: str
    slug: str
    items: list[ItemConfig]


@dataclass
class AlertsConfig:
    console: bool = True
    markdown_report: bool = True
    webhook_url: str = ""


@dataclass
class EbayConfig:
    enabled: bool = True
    app_id: str = ""
    cert_id: str = ""
    env: str = "production"  # production | sandbox


@dataclass
class SourcesConfig:
    ebay: EbayConfig = field(default_factory=EbayConfig)
    gumtree_enabled: bool = True
    facebook_enabled: bool = True

    def enabled_names(self) -> list[str]:
        names = []
        if self.ebay.enabled:
            names.append("ebay")
        if self.gumtree_enabled:
            names.append("gumtree")
        if self.facebook_enabled:
            names.append("facebook")
        return names


@dataclass
class AppConfig:
    postcode: str = ""
    radius_miles: int = 30
    interval_minutes: int = 60
    db_path: str = "data/product_finder.db"
    report_path: str = "reports/latest.md"
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    projects: list[ProjectConfig] = field(default_factory=list)


def _as_float(value, label: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{label} must be a number, got {value!r}")


def _load_item(raw: dict, project_slug: str) -> ItemConfig:
    name = raw.get("name")
    if not name:
        raise ConfigError(f"Item in project '{project_slug}' is missing 'name'")
    terms = raw.get("terms") or []
    if not terms:
        raise ConfigError(f"Item '{name}' has no search terms")
    sources = raw.get("sources")
    if sources is not None:
        unknown = [s for s in sources if s not in KNOWN_SOURCES]
        if unknown:
            raise ConfigError(f"Item '{name}' has unknown sources: {unknown}")
    priority = str(raw.get("priority", "normal")).lower()
    if priority not in ("high", "normal", "low"):
        raise ConfigError(f"Item '{name}' priority must be high/normal/low")
    return ItemConfig(
        name=str(name),
        terms=[str(t) for t in terms],
        max_price=_as_float(raw.get("max_price"), f"{name}.max_price"),
        normal_price=_as_float(raw.get("normal_price"), f"{name}.normal_price"),
        target_deal_price=_as_float(raw.get("target_deal_price"), f"{name}.target_deal_price"),
        priority=priority,
        notes=str(raw.get("notes", "")),
        exclude_terms=[str(t) for t in (raw.get("exclude_terms") or [])],
        sources=sources,
    )


def _load_project(raw: dict) -> ProjectConfig:
    name = raw.get("name")
    if not name:
        raise ConfigError("Project missing 'name'")
    slug = raw.get("slug") or str(name).lower().replace(" ", "-")
    items = [_load_item(i, slug) for i in (raw.get("items") or [])]
    if not items:
        raise ConfigError(f"Project '{slug}' has no items")
    return ProjectConfig(name=str(name), slug=str(slug), items=items)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. "
            "Copy config.example.yaml to config.yaml and edit it."
        )
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    alerts_raw = raw.get("alerts") or {}
    alerts = AlertsConfig(
        console=bool(alerts_raw.get("console", True)),
        markdown_report=bool(alerts_raw.get("markdown_report", True)),
        webhook_url=str(alerts_raw.get("webhook_url") or ""),
    )

    sources_raw = raw.get("sources") or {}
    ebay_raw = sources_raw.get("ebay") or {}
    sources = SourcesConfig(
        ebay=EbayConfig(
            enabled=bool(ebay_raw.get("enabled", True)),
            app_id=str(ebay_raw.get("app_id") or ""),
            cert_id=str(ebay_raw.get("cert_id") or ""),
            env=str(ebay_raw.get("env", "production")),
        ),
        gumtree_enabled=bool((sources_raw.get("gumtree") or {}).get("enabled", True)),
        facebook_enabled=bool((sources_raw.get("facebook") or {}).get("enabled", True)),
    )

    projects = [_load_project(p) for p in (raw.get("projects") or [])]
    slugs = [p.slug for p in projects]
    if len(slugs) != len(set(slugs)):
        raise ConfigError("Duplicate project slugs in config")

    return AppConfig(
        postcode=str(raw.get("postcode") or ""),
        radius_miles=int(raw.get("radius_miles", 30)),
        interval_minutes=int(raw.get("interval_minutes", 60)),
        db_path=str(raw.get("db_path", "data/product_finder.db")),
        report_path=str(raw.get("report_path", "reports/latest.md")),
        alerts=alerts,
        sources=sources,
        projects=projects,
    )
