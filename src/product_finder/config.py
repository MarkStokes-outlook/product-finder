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
    id: int | None = None  # set when loaded from the database


@dataclass
class ProjectConfig:
    name: str
    slug: str
    items: list[ItemConfig]
    sources: list[str] | None = None  # None = no project-level restriction
    id: int | None = None  # set when loaded from the database


@dataclass
class AlertsConfig:
    console: bool = True
    webhook_url: str = ""


@dataclass
class EbayConfig:
    enabled: bool = True
    app_id: str = ""
    cert_id: str = ""
    env: str = "production"  # production | sandbox


EXTRA_SOURCE_TYPES = ("rss", "links")


@dataclass
class ExtraSourceConfig:
    """A config-defined source: no code needed per site.

    type "rss"   — automated: fetch and parse an RSS/Atom feed per term.
    type "links" — manual-assisted: generate search links from a URL template.
    Templates may use {term}, {max_price}, {postcode}, {radius}.
    """

    name: str
    type: str
    url: str
    label: str = ""
    enabled: bool = True
    # rss only: drop entries older than this (their pubDate/updated/published).
    # Feeds like Reddit search keep old posts searchable indefinitely, so
    # without this a "deal" can be a 2-year-old thread for an item long since
    # sold. Entries with no parseable date are kept (nothing to filter on).
    max_age_days: int | None = None


@dataclass
class SourcesConfig:
    ebay: EbayConfig = field(default_factory=EbayConfig)
    gumtree_enabled: bool = True
    facebook_enabled: bool = True
    extra: list[ExtraSourceConfig] = field(default_factory=list)

    def enabled_names(self) -> list[str]:
        names = []
        if self.ebay.enabled:
            names.append("ebay")
        if self.gumtree_enabled:
            names.append("gumtree")
        if self.facebook_enabled:
            names.append("facebook")
        names.extend(e.name for e in self.extra if e.enabled)
        return names

    def all_names(self) -> list[str]:
        return list(KNOWN_SOURCES) + [e.name for e in self.extra]


@dataclass
class AppConfig:
    postcode: str = ""
    radius_miles: int = 30
    interval_minutes: int = 60
    db_path: str = "data/product_finder.db"
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
    sources = raw.get("sources")  # validated against all source names after load
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
    sources = raw.get("sources")  # validated against all source names after load
    return ProjectConfig(name=str(name), slug=str(slug), items=items, sources=sources)


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
        webhook_url=str(alerts_raw.get("webhook_url") or ""),
    )

    sources_raw = raw.get("sources") or {}
    ebay_raw = sources_raw.get("ebay") or {}
    extra = []
    for raw_extra in (sources_raw.get("extra") or []):
        ename = str(raw_extra.get("name") or "").strip().lower()
        if not ename:
            raise ConfigError("Extra source is missing 'name'")
        if ename in KNOWN_SOURCES or any(e.name == ename for e in extra):
            raise ConfigError(f"Duplicate source name: '{ename}'")
        etype = str(raw_extra.get("type") or "links").lower()
        if etype not in EXTRA_SOURCE_TYPES:
            raise ConfigError(
                f"Extra source '{ename}' has unknown type '{etype}' "
                f"(expected one of {EXTRA_SOURCE_TYPES})"
            )
        url = str(raw_extra.get("url") or "")
        if "{term}" not in url:
            raise ConfigError(f"Extra source '{ename}' url must contain {{term}}")
        max_age_raw = raw_extra.get("max_age_days")
        max_age_days = None
        if max_age_raw is not None:
            try:
                max_age_days = int(max_age_raw)
            except (TypeError, ValueError):
                raise ConfigError(f"Extra source '{ename}' max_age_days must be an integer")
            if max_age_days <= 0:
                raise ConfigError(f"Extra source '{ename}' max_age_days must be positive")
        extra.append(
            ExtraSourceConfig(
                name=ename,
                type=etype,
                url=url,
                label=str(raw_extra.get("label") or ""),
                enabled=bool(raw_extra.get("enabled", True)),
                max_age_days=max_age_days,
            )
        )
    sources = SourcesConfig(
        ebay=EbayConfig(
            enabled=bool(ebay_raw.get("enabled", True)),
            app_id=str(ebay_raw.get("app_id") or ""),
            cert_id=str(ebay_raw.get("cert_id") or ""),
            env=str(ebay_raw.get("env", "production")),
        ),
        gumtree_enabled=bool((sources_raw.get("gumtree") or {}).get("enabled", True)),
        facebook_enabled=bool((sources_raw.get("facebook") or {}).get("enabled", True)),
        extra=extra,
    )

    projects = [_load_project(p) for p in (raw.get("projects") or [])]
    slugs = [p.slug for p in projects]
    if len(slugs) != len(set(slugs)):
        raise ConfigError("Duplicate project slugs in config")
    allowed = set(sources.all_names())
    for project in projects:
        if project.sources is not None:
            unknown = [s for s in project.sources if s not in allowed]
            if unknown:
                raise ConfigError(f"Project '{project.name}' has unknown sources: {unknown}")
        for item in project.items:
            if item.sources is not None:
                unknown = [s for s in item.sources if s not in allowed]
                if unknown:
                    raise ConfigError(f"Item '{item.name}' has unknown sources: {unknown}")

    return AppConfig(
        postcode=str(raw.get("postcode") or ""),
        radius_miles=int(raw.get("radius_miles", 30)),
        interval_minutes=int(raw.get("interval_minutes", 60)),
        db_path=str(raw.get("db_path", "data/product_finder.db")),
        alerts=alerts,
        sources=sources,
        projects=projects,
    )
