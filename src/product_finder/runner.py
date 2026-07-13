"""One search cycle: fetch, dedupe, score, alert, report."""

from __future__ import annotations

import logging
import sqlite3

from . import catalogue, db, extraction, retailer_price, scoring, sources
from .alerts import console as console_alerts
from .alerts import webhook as webhook_alerts
from .config import AppConfig, ItemConfig, OllamaConfig, ProjectConfig
from .models import Listing, ManualLink, MatchAlert
from .orchestrator import SearchOrchestrator, WorkItem

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


def reassess_item_matches(conn: sqlite3.Connection, item_id: int, item: ItemConfig) -> dict:
    """Re-run catalogue matching and scoring for every listing already
    matched against this item, against its just-saved settings — called
    after an item edit so a newly-added exclude term, a changed max price,
    or a corrected normal/target price takes effect on existing matches
    immediately, not only on whatever the next watch cycle happens to
    re-fetch. scoring.excluded() and the max_price check only ever gated
    *new* matches at ingest time (see run_once above); nothing previously
    re-applied them to what's already stored.

    A listing that now fails exclusion or max_price — or was individually
    flagged "not a match" for this item (see db.exclude_listing_from_item)
    — is removed outright (db.delete_listing_match). Its price history
    lives in product_price_observations, keyed to the product, so nothing
    is lost. Everything else is re-scored and re-attributed to whichever
    catalogue product now matches (or none); db.record_match's upsert makes
    this idempotent to re-run. Returns {"rescored": n, "excluded": n}."""
    products = db.list_products_for_matching(conn, item_id)
    rows = conn.execute(
        "SELECT m.id AS match_id, l.* FROM listing_matches m "
        "JOIN listings l ON l.id = m.listing_id WHERE m.item_id = ?",
        (item_id,),
    ).fetchall()
    rescored = excluded = 0
    for row in rows:
        listing = Listing.from_row(row)
        if (
            scoring.excluded(listing, item)
            or (item.max_price and listing.price > item.max_price)
            or db.listing_excluded_from_item(conn, item_id, listing.source, listing.external_id)
        ):
            db.delete_listing_match(conn, row["match_id"])
            excluded += 1
            continue
        product = catalogue.match(listing.text, products) if products else None
        evaluation = scoring.evaluate(listing, item, product)
        db.record_match(
            conn, row["id"], item_id, evaluation,
            product_id=product.global_product_id if product else None,
        )
        rescored += 1
    return {"rescored": rescored, "excluded": excluded}


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


def run_once(
    cfg: AppConfig,
    conn: sqlite3.Connection,
    orchestrator: SearchOrchestrator | None = None,
) -> list[MatchAlert]:
    """Run one full cycle. Returns the new (not previously alerted) matches.

    `orchestrator` defaults to a SearchOrchestrator wrapping the built
    registry with DefaultExecutionPolicy — today's exact sequential,
    single-attempt, always-run behaviour (see orchestrator.py). This
    function no longer calls Source.search() directly; it builds the
    cycle's WorkItems and asks the orchestrator to execute them. Accepting
    one as a parameter is the seam a future scheduler/policy plugs into —
    without this function changing again."""
    cfg = db.effective_config(conn, cfg)
    projects = load_projects(cfg, conn)
    registry = sources.build_registry(cfg)
    if orchestrator is None:
        orchestrator = SearchOrchestrator(registry)
    new_alerts: list[MatchAlert] = []
    # Per-connector outcome for this cycle, recorded via record_source_run()
    # at the end — feeds the Sources page's health column and the coverage
    # metrics the roadmap wants ("active and failing sources").
    health: dict[str, dict] = {}

    for project in projects:
        for item in project.items:
            item_id = item.id
            products = db.list_products_for_matching(conn, item_id) if item_id else []
            work_items = [
                WorkItem(source_name=name, term=term, item=item)
                for name in item_sources(item, cfg, project)
                if (source := registry.get(name)) is not None and source.is_automated()
                for term in item.terms
            ]
            for outcome in orchestrator.run(work_items):
                name = outcome.source_name
                stats = health.setdefault(
                    name,
                    {
                        "searches": 0, "listings": 0, "errors": 0, "last_error": None,
                        "duration_ms": 0, "new_listings": 0, "duplicates": 0,
                        "catalogue_matches": 0, "deals_found": 0,
                    },
                )
                stats["searches"] += 1
                stats["duration_ms"] += outcome.duration_ms
                if outcome.error is not None:
                    # Source failures must never crash the run — the
                    # orchestrator already logged this; just record it.
                    stats["errors"] += 1
                    stats["last_error"] = str(outcome.error)
                    continue
                stats["listings"] += len(outcome.listings)
                source = registry[name]
                for listing in outcome.listings:
                    if scoring.excluded(listing, item):
                        continue
                    if item.max_price and listing.price > item.max_price:
                        continue
                    # A human already said this exact listing is wrong for
                    # this item (the "Not a match" button) — never recreate
                    # the match, even though nothing about its text/price
                    # would otherwise exclude it.
                    if item_id and db.listing_excluded_from_item(
                        conn, item_id, listing.source, listing.external_id
                    ):
                        continue
                    product = catalogue.match(listing.text, products) if products else None
                    evaluation = scoring.evaluate(listing, item, product)
                    if product is not None:
                        stats["catalogue_matches"] += 1
                    if evaluation.under_target:
                        stats["deals_found"] += 1
                    listing_id, is_new_listing = db.upsert_listing(conn, listing)
                    if is_new_listing:
                        stats["new_listings"] += 1
                    # Cross-source identity resolution (v1: canonical-URL
                    # matching only — see identity.py/resolve_identity()).
                    # is_primary is False only for a confirmed duplicate
                    # of a listing already counted elsewhere; it still
                    # gets its own listing_matches row below (full
                    # provenance), just no alert/observation/list surface.
                    _, is_primary = db.resolve_identity(conn, listing_id, listing)
                    if not is_primary:
                        stats["duplicates"] += 1
                    match_id, is_new = db.record_match(
                        conn, listing_id, item_id, evaluation,
                        product_id=product.global_product_id if product else None,
                    )
                    if is_new and is_primary and product and not scoring.is_live_auction(listing):
                        # One observation per distinct listing, at first
                        # sighting only — a long-unsold listing rescanned
                        # every cycle shouldn't dominate the average. Global
                        # product id: this observation feeds the shared
                        # catalogue entry, not just this item's view of it.
                        db.record_price_observation(
                            conn, product.global_product_id, listing.price, listing.source
                        )
                    if (
                        product is None
                        and item_id
                        and source.capabilities().supports_enrichment
                    ):
                        _maybe_suggest_product(conn, source, listing_id, item_id, cfg.ollama)
                    # Knowledge-only products (wanted=False) are
                    # identification, not endorsement: price history above
                    # still accumulates, but no alert fires and read-time
                    # gating (db._WANTED) keeps the match off deal surfaces.
                    if is_new and is_primary and (product is None or product.wanted):
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
    for name, stats in health.items():
        db.record_source_run(conn, name, **stats)
    retailer_price.run_discovery_and_refresh(conn, cfg)
    # Fuzzy duplicate detection (identity v2 — see duplicates.py): propose
    # probable same-physical-item pairs for human confirm/dismiss on the
    # project page. Proposals only; nothing is ever merged automatically.
    db.scan_duplicate_candidates(conn)
    conn.commit()

    new_alerts.sort(key=lambda a: a.evaluation.deal_score, reverse=True)
    _send_alerts(cfg, conn, new_alerts)
    return new_alerts


def _maybe_suggest_product(
    conn: sqlite3.Connection,
    source: sources.Source,
    listing_id: int,
    item_id: int,
    ollama_cfg: OllamaConfig,
) -> None:
    """A listing that didn't resolve to any known catalogue product is a
    chance to discover a new one — but only worth an extra API call once
    per listing ever, not on every rescan of the same still-unmatched one.

    Offered to any connector declaring supports_enrichment (not "to eBay" —
    connectors are capabilities, not special cases). Structured brand/model
    fields from get_item_details() are tried first (a much more reliable
    signal). Only when the structured *model* is absent — common with
    private/casual sellers who pick a brand from a dropdown but skip the
    free-text MPN field — does the optional Ollama free-text fallback get a
    look, over the listing's own title/description, never a second API
    round-trip. eBay's own brand is still kept over an LLM guess whenever
    it's available; only the model is filled in from extraction."""
    listing_row = db.get_listing(conn, listing_id)
    if listing_row is None or listing_row["brand_checked"]:
        return
    try:
        details = source.get_item_details(listing_row["external_id"])
    except Exception as exc:
        log.warning("Product-detail lookup failed for %s: %s", listing_row["external_id"], exc)
        details = None
    db.mark_brand_checked(conn, listing_id)
    if details and details["model"]:
        db.record_suggestion_sighting(
            conn, item_id, details["brand"], details["model"], listing_row["url"]
        )
        return
    text = " ".join(p for p in (listing_row["title"], listing_row["description"]) if p)
    extracted = extraction.extract_brand_model(text, ollama_cfg)
    if extracted:
        brand = details["brand"] if details else extracted["brand"]
        db.record_suggestion_sighting(
            conn, item_id, brand, extracted["model"], listing_row["url"],
            source="ollama",
        )
    elif details:
        # Structured brand but no recoverable model anywhere — record as
        # brand-only rather than dropping the sighting entirely.
        db.record_suggestion_sighting(
            conn, item_id, details["brand"], details["model"], listing_row["url"]
        )


def _send_alerts(cfg: AppConfig, conn: sqlite3.Connection, alerts: list[MatchAlert]) -> None:
    for alert in alerts:
        match_id = alert.extras["match_id"]
        if cfg.alerts.console and db.mark_alerted(conn, match_id, "console"):
            console_alerts.send(alert)
        if cfg.alerts.webhook_url and db.mark_alerted(conn, match_id, "webhook"):
            webhook_alerts.send(alert, cfg.alerts.webhook_url)
    conn.commit()
