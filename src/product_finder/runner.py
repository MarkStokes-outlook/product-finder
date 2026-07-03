"""One search cycle: fetch, dedupe, score, alert, report."""

from __future__ import annotations

import logging
import sqlite3

from . import catalogue, db, extraction, retailer_price, scoring, sources
from .alerts import console as console_alerts
from .alerts import webhook as webhook_alerts
from .config import AppConfig, ItemConfig, OllamaConfig, ProjectConfig
from .models import ManualLink, MatchAlert
from .sources.ebay import EbaySource

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
                        # Cross-source identity resolution (v1: canonical-URL
                        # matching only — see identity.py/resolve_identity()).
                        # is_primary is False only for a confirmed duplicate
                        # of a listing already counted elsewhere; it still
                        # gets its own listing_matches row below (full
                        # provenance), just no alert/observation/list surface.
                        _, is_primary = db.resolve_identity(conn, listing_id, listing)
                        match_id, is_new = db.record_match(
                            conn, listing_id, item_id, evaluation,
                            product_id=product.id if product else None,
                        )
                        if is_new and is_primary and product and not scoring.is_live_auction(listing):
                            # One observation per distinct listing, at first
                            # sighting only — a long-unsold listing rescanned
                            # every cycle shouldn't dominate the average.
                            db.record_price_observation(conn, product.id, listing.price, listing.source)
                        if product is None and item_id and isinstance(source, EbaySource):
                            _maybe_suggest_product(conn, source, listing_id, item_id, cfg.ollama)
                        if is_new and is_primary:
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
    retailer_price.run_discovery_and_refresh(conn, cfg)
    conn.commit()

    new_alerts.sort(key=lambda a: a.evaluation.deal_score, reverse=True)
    _send_alerts(cfg, conn, new_alerts)
    return new_alerts


def _maybe_suggest_product(
    conn: sqlite3.Connection,
    source: EbaySource,
    listing_id: int,
    item_id: int,
    ollama_cfg: OllamaConfig,
) -> None:
    """A listing that didn't resolve to any known catalogue product is a
    chance to discover a new one — but only worth an extra API call once
    per listing ever, not on every rescan of the same still-unmatched one.

    Structured eBay brand/mpn fields are tried first (a much more reliable
    signal). Only when those are absent — common with private/casual
    sellers — does the optional Ollama free-text fallback get a look, over
    the listing's own title/description, never a second API round-trip."""
    listing_row = db.get_listing(conn, listing_id)
    if listing_row is None or listing_row["brand_checked"]:
        return
    try:
        details = source.get_item_details(listing_row["external_id"])
    except Exception as exc:
        log.warning("Product-detail lookup failed for %s: %s", listing_row["external_id"], exc)
        details = None
    db.mark_brand_checked(conn, listing_id)
    if details:
        db.record_suggestion_sighting(
            conn, item_id, details["brand"], details["model"], listing_row["url"]
        )
        return
    text = " ".join(p for p in (listing_row["title"], listing_row["description"]) if p)
    extracted = extraction.extract_brand_model(text, ollama_cfg)
    if extracted:
        db.record_suggestion_sighting(
            conn, item_id, extracted["brand"], extracted["model"], listing_row["url"],
            source="ollama",
        )


def _send_alerts(cfg: AppConfig, conn: sqlite3.Connection, alerts: list[MatchAlert]) -> None:
    for alert in alerts:
        match_id = alert.extras["match_id"]
        if cfg.alerts.console and db.mark_alerted(conn, match_id, "console"):
            console_alerts.send(alert)
        if cfg.alerts.webhook_url and db.mark_alerted(conn, match_id, "webhook"):
            webhook_alerts.send(alert, cfg.alerts.webhook_url)
    conn.commit()
