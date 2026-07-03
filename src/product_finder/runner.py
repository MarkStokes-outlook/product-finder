"""One search cycle: fetch, dedupe, score, alert, report."""

from __future__ import annotations

import logging
import sqlite3

from . import catalogue, db, scoring, sources
from .alerts import console as console_alerts
from .alerts import webhook as webhook_alerts
from .config import AppConfig, ItemConfig, ProjectConfig
from .models import ManualLink, MatchAlert

log = logging.getLogger(__name__)


def item_sources(
    item: ItemConfig, cfg: AppConfig, project: ProjectConfig | None = None
) -> list[str]:
    """Sources actually searched for this item: enabled sources, narrowed by
    the project's allowed sources (if any), then by the item's own filter
    (if any). Either filter being None means "no extra restriction"."""
    enabled = cfg.sources.enabled_names()
    if project is not None and project.sources is not None:
        enabled = [s for s in enabled if s in project.sources]
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
            for name in item_sources(item, cfg, project):
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
            products = db.list_products_for_matching(conn, item_id) if item_id else []
            for name in item_sources(item, cfg, project):
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
                        product = catalogue.match(listing.text, products) if products else None
                        evaluation = scoring.evaluate(listing, item, product)
                        listing_id, _ = db.upsert_listing(conn, listing)
                        match_id, is_new = db.record_match(
                            conn, listing_id, item_id, evaluation,
                            product_id=product.id if product else None,
                        )
                        if is_new and product and not scoring.is_live_auction(listing):
                            # One observation per distinct listing, at first
                            # sighting only — a long-unsold listing rescanned
                            # every cycle shouldn't dominate the average.
                            db.record_price_observation(conn, product.id, listing.price, listing.source)
                        if is_new:
                            normal_price, target_deal_price, _ = scoring.effective_prices(item, product)
                            new_alerts.append(
                                MatchAlert(
                                    project_name=project.name,
                                    item_name=item.name,
                                    listing=listing,
                                    evaluation=evaluation,
                                    normal_price=normal_price,
                                    target_deal_price=target_deal_price,
                                    extras={"match_id": match_id},
                                )
                            )
    conn.commit()

    new_alerts.sort(key=lambda a: a.evaluation.deal_score, reverse=True)
    _send_alerts(cfg, conn, new_alerts)
    return new_alerts


def _send_alerts(cfg: AppConfig, conn: sqlite3.Connection, alerts: list[MatchAlert]) -> None:
    for alert in alerts:
        match_id = alert.extras["match_id"]
        if cfg.alerts.console and db.mark_alerted(conn, match_id, "console"):
            console_alerts.send(alert)
        if cfg.alerts.webhook_url and db.mark_alerted(conn, match_id, "webhook"):
            webhook_alerts.send(alert, cfg.alerts.webhook_url)
    conn.commit()
