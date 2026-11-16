"""One search cycle: fetch, dedupe, score, alert, report."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import deque
from collections.abc import Iterable
from dataclasses import replace

from . import catalogue, db, extraction, grading, relevance, retailer_price, scoring, sources
from .alerts import console as console_alerts
from .alerts import webhook as webhook_alerts
from .config import AppConfig, ItemConfig, OllamaConfig, ProjectConfig
from .models import Evaluation, Listing, ManualLink, MatchAlert, OfferCheck
from .orchestrator import SearchOrchestrator, WorkItem

log = logging.getLogger(__name__)

_OFFER_CHECKS_PER_CYCLE = 12
_VARIANT_GROUPS_PER_CYCLE = 2


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


def _assess(
    listing: Listing, item: ItemConfig, products: list[catalogue.Product],
    permitted_sources: Iterable[str] | None,
    *,
    manually_excluded: bool = False,
    offer: OfferCheck | None = None,
) -> tuple[relevance.Decision, Evaluation, catalogue.Product | None]:
    total = offer.total if offer else None
    if manually_excluded:
        decision = relevance.Decision("excluded", "This listing was dismissed for this watch.")
        product = None
    elif permitted_sources is not None and listing.source not in permitted_sources:
        decision = relevance.Decision("excluded", "The source is outside this watch's settings.")
        product = None
    elif scoring.excluded(listing, item):
        decision = relevance.Decision("excluded", "The title matches a watch exclusion.")
        product = None
    elif item.max_price is not None and (total if total is not None else listing.price) > item.max_price:
        decision = relevance.Decision(
            "excluded",
            "The delivered total exceeds the watch budget." if total is not None
            else "The listing price exceeds the watch budget.",
        )
        product = None
    else:
        decision, product = relevance.assess(listing, item, products)
    priced_listing = listing
    if total is not None:
        # Score the verified fixed-price purchase. The original listing still
        # carries the auction and bid for display and tracking.
        priced_listing = replace(listing, price=total, buying_options=[
            option for option in listing.buying_options if option != "AUCTION"
        ])
    evaluation = scoring.evaluate(priced_listing, item, product) if decision.eligible else Evaluation(
        grading.classify(listing.text), [], 0, 0, False, 0,
    )
    if total is None:
        evaluation.under_target = False
    return decision, evaluation, product


def reassess_item_matches(
    conn: sqlite3.Connection, item_id: int, item: ItemConfig, cfg: AppConfig | None = None,
) -> dict:
    """Re-evaluate stored candidates in place, retaining their evidence and alert history."""
    products = db.list_products_for_matching(conn, item_id)
    project_sources = conn.execute(
        "SELECT p.sources FROM projects p JOIN items i ON p.id = i.project_id WHERE i.id = ?",
        (item_id,),
    ).fetchone()
    permitted = set(cfg.sources.enabled_names()) if cfg is not None else None
    for restriction in (item.sources, json.loads(project_sources["sources"] or "null") if project_sources else None):
        if restriction is not None:
            permitted = permitted.intersection(restriction) if permitted is not None else set(restriction)

    rows = conn.execute(
        "SELECT l.*, x.item_id IS NOT NULL AS manually_excluded FROM listing_matches m "
        "JOIN listings l ON l.id = m.listing_id "
        "LEFT JOIN listing_match_exclusions x ON x.listing_id = m.listing_id AND x.item_id = m.item_id "
        "WHERE m.item_id = ?",
        (item_id,),
    ).fetchall()
    rescored = excluded = 0
    for row in rows:
        listing = Listing.from_row(row)
        decision, evaluation, product = _assess(
            listing, item, products, permitted, manually_excluded=bool(row["manually_excluded"]),
            offer=db.recent_offer_check(conn, row["id"]),
        )
        db.record_match(
            conn, row["id"], item_id, evaluation,
            product_id=product.global_product_id if product else None,
            match_status=decision.status, match_reason=decision.reason,
        )
        if decision.eligible:
            rescored += 1
        else:
            excluded += 1
    return {"rescored": rescored, "excluded": excluded}


def reassess_saved_matches(conn: sqlite3.Connection, cfg: AppConfig) -> dict:
    """Evaluate saved candidates without making requests or delivering alerts."""
    db.refresh_market_references(conn)
    counts = {"rescored": 0, "excluded": 0}
    for row in db.list_items(conn, include_archived=True):
        result = reassess_item_matches(conn, row["id"], db._item_from_row(row), cfg)
        for key in counts:
            counts[key] += result[key]
    return counts


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
    db.set_offer_destination(conn, cfg.postcode)
    db.refresh_market_references(conn)
    projects = load_projects(cfg, conn)
    registry = sources.build_registry(cfg)
    if orchestrator is None:
        orchestrator = SearchOrchestrator(registry)
    orchestrator.begin_cycle()
    new_alerts: list[MatchAlert] = []
    # Per-connector outcome for this cycle, recorded via record_source_run()
    # at the end — feeds the Sources page's health column and the coverage
    # metrics the roadmap wants ("active and failing sources").
    health: dict[str, dict] = {}
    query_rows = []
    checks_remaining = _OFFER_CHECKS_PER_CYCLE
    groups_remaining = _VARIANT_GROUPS_PER_CYCLE
    resolved_groups: dict[tuple[str, str, float], list[tuple[Listing, OfferCheck]]] = {}

    for project in projects:
        for item in project.items:
            checks_for_item = 0
            groups_for_item: set[tuple[str, str, float]] = set()
            capacity = relevance.required_nvme_capacity(item)
            item_id = item.id
            products = db.list_products_for_matching(conn, item_id) if item_id else []
            work_items = [
                WorkItem(source_name=name, term=term, item=item)
                for name in item_sources(item, cfg, project)
                if (source := registry.get(name)) is not None and source.is_automated()
                for term in dict.fromkeys(item.terms)
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
                relevant_ids = set()
                if outcome.error is not None:
                    # Source failures must never crash the run — the
                    # orchestrator already logged this; just record it.
                    stats["errors"] += 1
                    stats["last_error"] = str(outcome.error)
                    query_rows.append((item_id, name, outcome.term, outcome.attempts, outcome.duration_ms,
                                       0, 0, 0, None, None, True))
                    continue
                stats["listings"] += len(outcome.listings)
                source = registry[name]
                capabilities = source.capabilities()
                candidates = deque((listing, None) for listing in outcome.listings)
                while candidates:
                    listing, resolved_offer = candidates.popleft()
                    # A human already said this exact listing is wrong for
                    # this item (the "Not a match" button) — never recreate
                    # the match, even though nothing about its text/price
                    # would otherwise exclude it.
                    if item_id and db.listing_excluded_from_item(
                        conn, item_id, listing.source, listing.external_id
                    ):
                        continue
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
                    offer = db.recent_offer_check(conn, listing_id)
                    if (is_primary and listing.variation_group and listing.variation_group_id and capacity
                            and capabilities.supports_offer_verification):
                        key = listing.source, listing.variation_group_id, capacity
                        if key not in groups_for_item:
                            groups_for_item.add(key)
                            if key not in resolved_groups and groups_remaining and offer is None:
                                groups_remaining -= 1
                                resolved_groups[key] = source.resolve_nvme_variants(listing, capacity)
                                db.store_offer_check(
                                    conn, listing_id, listing,
                                    OfferCheck("review", "Use a separately priced variant from this group."),
                                    postcode=cfg.postcode,
                                )
                            candidates.extend(resolved_groups.get(key, []))
                    if is_primary and resolved_offer is not None:
                        db.store_offer_check(conn, listing_id, listing, resolved_offer, postcode=cfg.postcode)
                        offer = db.recent_offer_check(conn, listing_id)
                    decision, evaluation, product = _assess(listing, item, products, None, offer=offer)
                    checked_now = bool(resolved_offer and offer and offer.total is not None)
                    blocking_flags = set(evaluation.flags)
                    if ("FIXED_PRICE" in listing.buying_options and listing.end_time
                            and listing.buy_it_now_price == listing.price):
                        blocking_flags.discard(scoring.FLAG_LIVE_AUCTION)
                    if (is_primary and decision.eligible and offer is None and checks_remaining
                            and checks_for_item < 2 and capabilities.supports_offer_verification
                            and not blocking_flags and evaluation.grade != grading.SPARES
                            and (product is None or product.wanted)):
                        checks_remaining -= 1
                        checks_for_item += 1
                        offer = source.verify_offer(listing)
                        db.store_offer_check(conn, listing_id, listing, offer, postcode=cfg.postcode)
                        offer = db.recent_offer_check(conn, listing_id)
                        decision, evaluation, product = _assess(listing, item, products, None, offer=offer)
                        checked_now = offer is not None and offer.total is not None
                    if product is not None:
                        stats["catalogue_matches"] += 1
                    if evaluation.under_target:
                        stats["deals_found"] += 1
                    if decision.eligible:
                        relevant_ids.add(listing.external_id)
                    match_id, is_new = db.record_match(
                        conn, listing_id, item_id, evaluation,
                        product_id=product.global_product_id if product else None,
                        match_status=decision.status, match_reason=decision.reason,
                        search_term=outcome.term,
                    )
                    if decision.status == "excluded":
                        continue
                    if is_new and is_primary and product and not scoring.is_live_auction(listing):
                        # One observation per distinct listing, at first
                        # sighting only — a long-unsold listing rescanned
                        # every cycle shouldn't dominate the average. Global
                        # product id: this observation feeds the shared
                        # catalogue entry, not just this item's view of it.
                        db.record_price_observation(
                            conn, product.global_product_id, listing.price, listing.source, listing_id=listing_id,
                        )
                    if (
                        product is None
                        and item_id
                        and capabilities.supports_enrichment
                    ):
                        _maybe_suggest_product(conn, source, listing_id, item_id, cfg.ollama)
                    # Knowledge-only products (wanted=False) are
                    # identification, not endorsement: price history above
                    # still accumulates, but no alert fires and read-time
                    # gating (db._WANTED) keeps the match off deal surfaces.
                    if (decision.eligible and offer is not None and offer.total is not None
                            and (is_new or checked_now) and is_primary and (product is None or product.wanted)):
                        normal_price, target_deal_price, _ = scoring.effective_prices(item, product)
                        new_alerts.append(
                            MatchAlert(
                                project_name=project.name,
                                item_name=item.name,
                                listing=replace(listing, price=offer.total),
                                evaluation=evaluation,
                                normal_price=normal_price,
                                target_deal_price=target_deal_price,
                                extras={
                                    "match_id": match_id,
                                    "purchase_mode": (
                                        "buy_it_now" if scoring.is_live_auction(listing) else "fixed_price"
                                    ),
                                    "postage": offer.postage,
                                },
                            )
                        )
                query_rows.append((
                    item_id, name, outcome.term, outcome.attempts, outcome.duration_ms, outcome.returned,
                    len({listing.external_id for listing in outcome.listings}),
                    len(relevant_ids.intersection(listing.external_id for listing in outcome.listings)),
                    outcome.total, outcome.has_more, False,
                ))
    db.record_search_queries(conn, query_rows)
    for name, stats in health.items():
        db.record_source_run(conn, name, **stats)
    retailer_price.run_discovery_and_refresh(conn, cfg)
    # Fuzzy duplicate detection (identity v2 — see duplicates.py): propose
    # probable same-physical-item pairs for human confirm/dismiss on the
    # project page. Proposals only; nothing is ever merged automatically.
    db.scan_duplicate_candidates(conn)
    conn.commit()

    new_alerts = [
        alert for alert in new_alerts
        if db.match_is_current(conn, alert.extras["match_id"], verified_only=True)
    ]
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
