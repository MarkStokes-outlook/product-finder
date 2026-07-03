"""One search cycle: fetch, dedupe, score, alert, report."""

from __future__ import annotations

import logging
import sqlite3

from . import db, scoring, sources
from .alerts import console as console_alerts
from .alerts import markdown as markdown_report
from .alerts import webhook as webhook_alerts
from .config import AppConfig, ItemConfig, ProjectConfig
from .models import ManualLink, MatchAlert

log = logging.getLogger(__name__)


def item_sources(item: ItemConfig, cfg: AppConfig) -> list[str]:
    enabled = cfg.sources.enabled_names()
    if item.sources is None:
        return enabled
    return [s for s in item.sources if s in enabled]


def load_projects(cfg: AppConfig, conn: sqlite3.Connection) -> list[ProjectConfig]:
    """Active projects/items from the DB, seeding from YAML on first use."""
    db.seed_from_config_if_empty(conn, cfg)
    return db.load_project_configs(conn)


def collect_manual_links(
    cfg: AppConfig,
    projects: list[ProjectConfig],
    registry: dict[str, sources.Source] | None = None,
) -> list[ManualLink]:
    registry = registry if registry is not None else sources.build_registry(cfg)
    links: list[ManualLink] = []
    for project in projects:
        for item in project.items:
            for name in item_sources(item, cfg):
                source = registry.get(name)
                if source is not None and not source.is_automated():
                    links.extend(source.manual_links(item))
    return links


def run_once(cfg: AppConfig, conn: sqlite3.Connection) -> list[MatchAlert]:
    """Run one full cycle. Returns the new (not previously alerted) matches."""
    cfg = db.effective_config(conn, cfg)
    projects = load_projects(cfg, conn)
    registry = sources.build_registry(cfg)
    new_alerts: list[MatchAlert] = []

    for project in projects:
        for item in project.items:
            item_id = item.id
            for name in item_sources(item, cfg):
                source = registry.get(name)
                if source is None or not source.is_automated():
                    continue
                for term in item.terms:
                    try:
                        listings = source.search(term, item)
                    except Exception as exc:
                        # Source failures must never crash the run.
                        log.warning("%s search failed for %r: %s", name, term, exc)
                        continue
                    for listing in listings:
                        if scoring.excluded(listing, item):
                            continue
                        if item.max_price and listing.price > item.max_price:
                            continue
                        evaluation = scoring.evaluate(listing, item)
                        listing_id, _ = db.upsert_listing(conn, listing)
                        match_id, is_new = db.record_match(conn, listing_id, item_id, evaluation)
                        if is_new:
                            new_alerts.append(
                                MatchAlert(
                                    project_name=project.name,
                                    item_name=item.name,
                                    listing=listing,
                                    evaluation=evaluation,
                                    normal_price=item.normal_price,
                                    target_deal_price=item.target_deal_price,
                                    extras={"match_id": match_id},
                                )
                            )
    conn.commit()

    new_alerts.sort(key=lambda a: a.evaluation.deal_score, reverse=True)
    _send_alerts(cfg, conn, new_alerts)

    if cfg.alerts.markdown_report:
        links = collect_manual_links(cfg, projects, registry)
        path = markdown_report.write_report(conn, cfg, links)
        log.info("Report written to %s", path)
    return new_alerts


def _send_alerts(cfg: AppConfig, conn: sqlite3.Connection, alerts: list[MatchAlert]) -> None:
    for alert in alerts:
        match_id = alert.extras["match_id"]
        if cfg.alerts.console and db.mark_alerted(conn, match_id, "console"):
            console_alerts.send(alert)
        if cfg.alerts.webhook_url and db.mark_alerted(conn, match_id, "webhook"):
            webhook_alerts.send(alert, cfg.alerts.webhook_url)
    conn.commit()
