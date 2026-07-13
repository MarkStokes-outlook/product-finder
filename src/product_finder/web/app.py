"""Local web UI. Flask, server-rendered, localhost only, no auth by design."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import (
    auction_trajectory,
    catalogue,
    connector_health,
    db,
    offers,
    outbound,
    project_import,
    retailer_price,
    runner,
    sources,
)
from ..config import AppConfig, ItemConfig

log = logging.getLogger(__name__)

# Deals scoring at or above this are "hot" — matches the excellent/hi score
# band used throughout the templates (score >= 70 -> green "hi" badge).
HOT_DEAL_SCORE = 70

# Surfaces the Marketplace Outbound Gateway (outbound.py) records a click
# against — see listing_clicks.context. A request with any other value (or
# none) is recorded as "unknown" rather than rejected: context is an audit
# label, not something worth failing a redirect over.
CLICK_CONTEXTS = ("dashboard", "project", "auctions", "offers", "duplicate_review")


def _get_conn(cfg: AppConfig):
    if "conn" not in g:
        g.conn = db.connect(cfg.db_path)
    return g.conn


def _effective_cfg(cfg: AppConfig) -> AppConfig:
    """cfg with DB-stored source overrides applied, resolved once per request
    so a Sources-page change takes effect on the very next page load."""
    if "effective_cfg" not in g:
        g.effective_cfg = db.effective_config(_get_conn(cfg), cfg)
    return g.effective_cfg


def _outbound_service(cfg: AppConfig) -> outbound.MarketplaceOutboundService:
    """Cached per-request like _effective_cfg() — affiliate config can only
    change between requests (config.yaml / env), never mid-request, but
    rebuilding once per request (not once per app start) keeps this
    consistent with how every other effective-config-derived value here
    behaves, and costs nothing measurable."""
    if "outbound_service" not in g:
        g.outbound_service = outbound.MarketplaceOutboundService(_effective_cfg(cfg))
    return g.outbound_service


def _selected_sources(form, source_names: list[str]) -> list[str] | None:
    """Parse `source_<name>` checkboxes. All ticked (or none) means "no
    restriction" — the caller should search every enabled source."""
    selected = [s for s in source_names if form.get(f"source_{s}")]
    return selected if selected and set(selected) != set(source_names) else None


def _item_from_form(form, source_names: list[str]) -> tuple[ItemConfig | None, list[str]]:
    """Parse the item form. Returns (item, errors)."""
    errors = []
    name = (form.get("name") or "").strip()
    if not name:
        errors.append("Name is required.")
    terms = [t.strip() for t in (form.get("terms") or "").splitlines() if t.strip()]
    if not terms:
        errors.append("At least one search term is required.")
    exclude_terms = [
        t.strip() for t in (form.get("exclude_terms") or "").splitlines() if t.strip()
    ]

    def parse_price(field: str) -> float | None:
        raw = (form.get(field) or "").strip().lstrip("£")
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            errors.append(f"{field.replace('_', ' ').capitalize()} must be a number.")
            return None
        if value < 0:
            errors.append(f"{field.replace('_', ' ').capitalize()} cannot be negative.")
        return value

    priority = form.get("priority", "normal")
    if priority not in ("high", "normal", "low"):
        priority = "normal"

    item = ItemConfig(
        name=name,
        terms=terms,
        max_price=parse_price("max_price"),
        normal_price=parse_price("normal_price"),
        target_deal_price=parse_price("target_deal_price"),
        priority=priority,
        notes=(form.get("notes") or "").strip(),
        exclude_terms=exclude_terms,
        sources=_selected_sources(form, source_names),
    )
    return (item if not errors else None), errors


def _product_from_form(form) -> tuple[dict | None, list[str]]:
    """Parse the catalogue product form. Returns (fields, errors)."""
    errors = []
    manufacturer = (form.get("manufacturer") or "").strip()
    if not manufacturer:
        errors.append("Manufacturer is required.")
    model = (form.get("model") or "").strip()
    match_terms = [t.strip() for t in (form.get("match_terms") or "").splitlines() if t.strip()]
    if not match_terms:
        errors.append("At least one match term is required.")

    def parse_price(field: str) -> float | None:
        raw = (form.get(field) or "").strip().lstrip("£")
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            errors.append(f"{field.replace('_', ' ').capitalize()} must be a number.")
            return None
        if value < 0:
            errors.append(f"{field.replace('_', ' ').capitalize()} cannot be negative.")
        return value

    fields = {
        "manufacturer": manufacturer,
        "model": model,
        "match_terms": match_terms,
        "msrp": parse_price("msrp"),
        "typical_new_price": parse_price("typical_new_price"),
        "target_deal_price": parse_price("target_deal_price"),
    }
    return (fields if not errors else None), errors


def _dashboard_data(conn, cfg: AppConfig) -> dict:
    return {
        "summaries": db.project_summaries(conn),
        "top_picks": db.project_top_picks(conn),
        "best": db.query_matches(conn, flagged=False, sort="score", limit=11),
        "warnings": db.query_matches(conn, flagged=True, sort="score", limit=10),
        "stats": db.dashboard_stats(conn),
        "pending_duplicates": db.pending_duplicate_counts(conn),
    }


# Maps auction_trajectory labels to a badge CSS modifier class — computed in
# Python, not Jinja string-matching, since labels are plain-English sentences.
_TRAJECTORY_CSS = {
    auction_trajectory.LABEL_INSUFFICIENT_DATA: "traj-unknown",
    auction_trajectory.LABEL_EARLY_WATCH: "traj-early",
    auction_trajectory.LABEL_POTENTIAL_DEAL: "traj-potential",
    auction_trajectory.LABEL_LIKELY_BARGAIN: "traj-bargain",
    auction_trajectory.LABEL_GETTING_HOT: "traj-hot",
    auction_trajectory.LABEL_NO_LONGER_DEAL: "traj-no-deal",
}


def _auction_view(conn, row: dict) -> dict:
    """Resolve one listings-row into its trajectory view-model: reference
    price prefers the product's typical_used_price (real market evidence)
    over the item's blended normal_price estimate, same convention as
    scoring.effective_prices().

    current_bid must never fall back to row['price'] — bug fixed 2026-07-08:
    for a BIN+AUCTION listing, row['price'] is the BIN-preferring
    _price_value() fallback (see sources/ebay.py), so falling back to it
    here displayed the Buy It Now price mislabelled as "current bid". The
    fix: row['current_bid_price'] is now populated at search time (real
    captures confirm currentBidPrice is present even at zero bids, equal to
    minimumPriceToBid then — never absent, so no further "starting price"
    fallback is needed), so the only remaining preference is snapshot
    history (freshest observed) over the listing's own last-seen value."""
    remaining = None
    if row["end_time"]:
        end_time = datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))
        remaining = end_time - datetime.now(timezone.utc)
    snapshots = db.list_auction_snapshots(conn, row["listing_id"])
    current_bid = (
        snapshots[-1]["current_bid_price"] if snapshots else row["current_bid_price"]
    )
    reference_price = row["typical_used_price"] or row["normal_price"]
    reference_label = "typical used price" if row["typical_used_price"] else "estimated normal price"
    trajectory = auction_trajectory.evaluate(
        current_bid=current_bid,
        bid_count=row["bid_count"],
        remaining=remaining,
        reference_price=reference_price,
        reference_label=reference_label,
        snapshots=snapshots,
    )
    return {
        "row": row,
        "current_bid": current_bid,
        "buy_it_now_price": row["buy_it_now_price"],
        "trajectory": trajectory,
        "trajectory_css": _TRAJECTORY_CSS.get(trajectory.label, "traj-unknown"),
    }


def _auctions_data(conn) -> dict:
    return {"auctions": [_auction_view(conn, row) for row in db.list_active_auctions(conn, limit=200)]}


def _offer_view(row: dict) -> dict:
    reference_price = row["typical_used_price"] or row["normal_price"]
    reference_label = "typical used price" if row["typical_used_price"] else "estimated normal price"
    suggestion = offers.suggest_offers(
        listing_price=row["price"],
        reference_price=reference_price,
        reference_label=reference_label,
        grade=row["grade"],
        verified=row["typical_used_price"] is not None,
        supports_offers=True,  # list_offer_listings already filtered to BEST_OFFER
    )
    return {"row": row, "suggestion": suggestion}


def _offers_data(conn) -> dict:
    return {"offers": [_offer_view(row) for row in db.list_offer_listings(conn, limit=200)]}


def _match_filters_from_request() -> dict:
    flagged_raw = request.args.get("flagged", "")
    return {
        "item_id": request.args.get("item_id", type=int),
        "source": request.args.get("source") or None,
        "grade": request.args.get("grade") or None,
        "flagged": {"yes": True, "no": False}.get(flagged_raw),
        "flagged_raw": flagged_raw,
        "sort": request.args.get("sort", "score"),
    }


def _project_detail_data(
    conn, cfg: AppConfig, eff_cfg: AppConfig, project_id: int, filters: dict | None = None
) -> dict | None:
    project = db.get_project(conn, project_id)
    if project is None:
        return None
    items = db.list_items(conn, project_id=project_id, include_archived=False)
    f = filters or {}
    # Query each item's matches separately (rather than one project-wide query
    # split afterwards) so an item with hundreds of listings can't starve a
    # sibling item's table of its own top results. Each table is paginated
    # client-side, so a generous per-item cap is cheap to render.
    selected_item_id = f.get("item_id")
    matches_by_item: dict[int, list] = {}
    for item in items:
        if selected_item_id and selected_item_id != item["id"]:
            matches_by_item[item["id"]] = []
            continue
        matches_by_item[item["id"]] = db.query_matches(
            conn,
            project_id=project_id,
            item_id=item["id"],
            source=f.get("source"),
            grade=f.get("grade"),
            flagged=f.get("flagged"),
            sort=f.get("sort", "score"),
            limit=500,
        )
    # Hero deal(s) for the callout — always the true top scores, independent
    # of whatever filters the listings below are currently narrowed by. Only
    # show more than one card when multiple listings clear the "hot deal"
    # bar; otherwise fall back to just the single best match as before.
    # flagged=False excludes anything with a warning flag — including live
    # auctions, whose "price" is just a current bid, not one you can commit
    # to, so a hero card should never headline one (same rule the dashboard
    # already applies to its own hero picks).
    top_matches = db.query_matches(conn, project_id=project_id, sort="score", flagged=False, limit=4)
    hot = [m for m in top_matches if (m["deal_score"] or 0) >= HOT_DEAL_SCORE]
    hero_deals = hot if len(hot) > 1 else top_matches[:1]
    project_cfg = next(
        (p for p in db.load_project_configs(conn) if p.id == project_id), None
    )
    manual_links = runner.collect_manual_links(eff_cfg, [project_cfg]) if project_cfg else []
    return {
        "project": project,
        "items": items,
        "matches_by_item": matches_by_item,
        "manual_links": manual_links,
        "hero_deals": hero_deals,
        "filters": f,
        # Display cap: the initial backlog on real data runs to hundreds of
        # pairs per project — render only the top slice by confidence and let
        # the queue drain decision by decision (the heading shows the total).
        "duplicates_pending": db.list_duplicate_candidates(conn, project_id=project_id, limit=30),
        "duplicates_pending_total": db.pending_duplicate_counts(conn).get(project_id, 0),
        "duplicates_decided": sorted(
            db.list_duplicate_candidates(conn, project_id=project_id, status="confirmed", limit=50)
            + db.list_duplicate_candidates(conn, project_id=project_id, status="dismissed", limit=50),
            key=lambda r: r["decided_at"] or "",
            reverse=True,
        ),
    }


def create_app(cfg: AppConfig) -> Flask:
    app = Flask(__name__)
    # Static key: localhost-only tool with no auth; sessions carry flash messages only.
    app.secret_key = "product-finder-local-ui"

    seed_conn = db.connect(cfg.db_path)
    db.seed_from_config_if_empty(seed_conn, cfg)
    seed_conn.close()

    @app.teardown_appcontext
    def _close_conn(exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def _globals():
        return {"cfg": cfg, "known_sources": _effective_cfg(cfg).sources.enabled_names()}

    @app.template_filter("parse_flags")
    def _parse_flags(raw):
        return json.loads(raw or "[]")

    @app.template_filter("timeago")
    def _timeago(raw):
        """ISO timestamp -> compact 'how long ago' ('just now', '3h ago',
        '2d ago'). Empty string when unknown/unparseable."""
        if not raw:
            return ""
        try:
            then = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return ""
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        seconds = (datetime.now(timezone.utc) - then).total_seconds()
        if seconds < 90:
            return "just now"
        if seconds < 5400:
            return f"{round(seconds / 60)}m ago"
        if seconds < 129600:
            return f"{round(seconds / 3600)}h ago"
        return f"{round(seconds / 86400)}d ago"

    @app.template_filter("guess_accessory_term")
    def _guess_accessory_term(title):
        """Pre-fills the "This is a part" prompt with a plausible exclude
        term — whichever ACCESSORY_KEYWORDS word the listing's own title
        already uses, or "" to leave the prompt blank rather than guess
        wrong when nothing matches."""
        return catalogue.find_accessory_keyword(title) or ""

    @app.template_filter("is_new")
    def _is_new(raw):
        """True when an ISO timestamp is within the last 24 hours — drives the
        NEW badge on deal cards and listing rows."""
        if not raw:
            return False
        try:
            then = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return False
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then) <= timedelta(hours=24)

    @app.template_global("listing_out_url")
    def _listing_out_url(listing_id, context: str, project_id=None) -> str:
        """Every listing link in every template calls this instead of
        rendering a listings.url straight into an href — see outbound.py
        and ARCHITECTURE.md ("Marketplace outbound gateway")."""
        return url_for(
            "listing_out", listing_id=listing_id, context=context, project_id=project_id
        )

    # --- Dashboard -----------------------------------------------------------

    @app.route("/")
    def dashboard():
        conn = _get_conn(cfg)
        return render_template(
            "dashboard.html",
            last_activity=db.latest_activity(conn),
            **_dashboard_data(conn, cfg),
        )

    @app.route("/dashboard/live")
    def dashboard_live():
        # Fragment-only render (no base.html) for the polling JS in
        # dashboard.html to swap in — no full page reload.
        conn = _get_conn(cfg)
        return render_template("_dashboard_live.html", **_dashboard_data(conn, cfg))

    @app.route("/api/status")
    def api_status():
        return {"last_activity": db.latest_activity(_get_conn(cfg))}

    # --- Marketplace Outbound Gateway -----------------------------------------
    # See outbound.py and ARCHITECTURE.md ("Marketplace outbound gateway").
    # The only place this app emits a marketplace URL — every listing link
    # in every template routes through here instead of rendering
    # listings.url directly.

    @app.route("/out/<int:listing_id>")
    def listing_out(listing_id):
        conn = _get_conn(cfg)
        listing_row = db.get_listing(conn, listing_id)
        if listing_row is None:
            abort(404)

        context = request.args.get("context", "")
        if context not in CLICK_CONTEXTS:
            context = "unknown"
        project_id = request.args.get("project_id", type=int)

        resolution = _outbound_service(cfg).resolve(listing_row["source"], listing_row["url"])
        outcome = "success" if outbound.is_safe_redirect_url(resolution.url) else "failure"
        if outcome == "failure":
            log.warning(
                "Unsafe outbound redirect destination for listing %s (source %r); refusing",
                listing_id, listing_row["source"],
            )

        try:
            db.record_listing_click(
                conn,
                listing_id=listing_id,
                source=listing_row["source"],
                context=context,
                outcome=outcome,
                affiliate_applied=resolution.affiliate_applied,
                project_id=project_id,
            )
        except Exception:
            # Analytics must never block the user's navigation — see
            # db.record_listing_click and FEATURE-1012.
            log.exception("Failed to record click for listing %s", listing_id)

        if outcome == "failure":
            abort(502)
        return redirect(resolution.url, code=302)

    # --- Match corrections (mismatched listings, spotted anywhere in the UI) -----
    # Two different fixes for the same complaint ("this shouldn't be scored
    # as my best deal"): a listing can be individually wrong for an item
    # with no shared pattern worth acting on ("Not a match"), or it can be
    # an accessory/part that other similar listings will keep repeating
    # ("This is a part" — adds an exclude term instead of a one-off fix).

    def _match_redirect():
        next_url = request.form.get("next", "")
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("dashboard"))

    @app.route("/matches/<int:match_id>/unmatch", methods=["POST"])
    def match_unmatch(match_id):
        conn = _get_conn(cfg)
        match = db.get_listing_match(conn, match_id)
        if match is None:
            abort(404)
        db.exclude_listing_from_item(conn, match["listing_id"], match["item_id"])
        flash("This listing will no longer be matched to this item.")
        return _match_redirect()

    @app.route("/items/<int:item_id>/exclude-term", methods=["POST"])
    def item_add_exclude_term(item_id):
        conn = _get_conn(cfg)
        row = db.get_item(conn, item_id)
        if row is None:
            abort(404)
        term = (request.form.get("term") or "").strip()
        if not term:
            flash("No exclude term given — nothing changed.")
            return _match_redirect()
        item = db._item_from_row(row)
        if term.lower() not in {t.lower() for t in item.exclude_terms}:
            item.exclude_terms = item.exclude_terms + [term]
            db.update_item(conn, item_id, item)
        result = runner.reassess_item_matches(conn, item_id, item)
        message = f'Added exclude term "{term}".'
        if result["rescored"] or result["excluded"]:
            message += f" Removed {result['excluded']} now-excluded match(es)."
        flash(message)
        return _match_redirect()

    # --- Active Auctions / Offers ------------------------------------------------

    @app.route("/auctions")
    def auctions():
        conn = _get_conn(cfg)
        return render_template("auctions.html", **_auctions_data(conn))

    @app.route("/offers")
    def offers_view():
        conn = _get_conn(cfg)
        return render_template("offers.html", **_offers_data(conn))

    # --- Sources ---------------------------------------------------------------

    @app.route("/sources")
    def source_list():
        eff_cfg = _effective_cfg(cfg)
        sc = eff_cfg.sources
        registry = sources.build_registry(eff_cfg)
        connectors = sources.build_all(eff_cfg)
        conn = _get_conn(cfg)
        health = db.source_health(conn)
        coverage = db.source_coverage(conn)
        analytics = db.source_coverage_analytics(conn)
        rows = [
            {"name": "ebay", "kind": "builtin", "label": "eBay",
             "enabled": sc.ebay.enabled,
             "automated": registry["ebay"].is_automated() if "ebay" in registry else False,
             "url": "", "max_age_days": None},
            {"name": "gumtree", "kind": "builtin", "label": "Gumtree",
             "enabled": sc.gumtree_enabled, "automated": False,
             "url": "", "max_age_days": None},
            {"name": "facebook", "kind": "builtin", "label": "Facebook Marketplace",
             "enabled": sc.facebook_enabled, "automated": False,
             "url": "", "max_age_days": None},
        ]
        for e in sc.extra:
            rows.append({
                "name": e.name, "kind": e.type, "label": e.label or e.name,
                "enabled": e.enabled,
                "automated": registry[e.name].is_automated() if e.name in registry else e.type == "rss",
                "url": e.url, "max_age_days": e.max_age_days,
            })
        for row in rows:
            row["caps"] = connectors[row["name"]].capabilities()
            row["knowledge"] = connectors[row["name"]].knowledge()
            row["health"] = health.get(row["name"])
            row["coverage"] = coverage.get(row["name"])
            row["analytics"] = analytics.get(row["name"])
            # Health *status* only makes sense once a source has actually
            # run — a source with no rows yet is "not yet run", a distinct
            # UI state this module doesn't classify (see connector_health's
            # own docstring).
            row["health_report"] = (
                connector_health.evaluate(row["health"], row["analytics"])
                if row["health"] else None
            )
        return render_template("sources.html", rows=rows, ebay=sc.ebay)

    @app.route("/sources/<name>/toggle", methods=["POST"])
    def source_toggle(name):
        conn = _get_conn(cfg)
        eff_cfg = _effective_cfg(cfg)
        if name not in eff_cfg.sources.all_names():
            abort(404)
        currently_enabled = name in eff_cfg.sources.enabled_names()
        db.set_source_enabled(conn, name, not currently_enabled)
        flash(f"'{name}' {'disabled' if currently_enabled else 'enabled'}.")
        return redirect(url_for("source_list"))

    @app.route("/sources/ebay/keys", methods=["POST"])
    def source_ebay_keys():
        conn = _get_conn(cfg)
        app_id = (request.form.get("app_id") or "").strip()
        cert_id = (request.form.get("cert_id") or "").strip()
        env = request.form.get("env") or "production"
        if env not in ("production", "sandbox"):
            env = "production"
        db.set_ebay_credentials(conn, app_id, cert_id, env)
        flash("eBay credentials saved.")
        return redirect(url_for("source_list"))

    # --- Projects ------------------------------------------------------------

    @app.route("/projects")
    def projects():
        conn = _get_conn(cfg)
        return render_template("projects.html", projects=db.list_projects(conn))

    @app.route("/projects/new", methods=["GET", "POST"])
    def project_new():
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Project name is required.")
            else:
                source_names = _effective_cfg(cfg).sources.enabled_names()
                proj_sources = _selected_sources(request.form, source_names)
                db.create_project(_get_conn(cfg), name, proj_sources)
                flash(f"Project '{name}' created.")
                return redirect(url_for("projects"))
        return render_template(
            "project_form.html", project=None, form=request.form if request.method == "POST" else None
        )

    @app.route("/projects/<int:project_id>")
    def project_detail(project_id):
        conn = _get_conn(cfg)
        data = _project_detail_data(
            conn, cfg, _effective_cfg(cfg), project_id, _match_filters_from_request()
        )
        if data is None:
            abort(404)
        return render_template(
            "project_detail.html", last_activity=db.latest_activity(conn), **data
        )

    @app.route("/projects/<int:project_id>/live")
    def project_detail_live(project_id):
        # Fragment-only render for the polling JS to swap in — same pattern
        # as /dashboard/live. Filters come from the query string so a
        # filtered view keeps auto-refreshing within the same filter.
        conn = _get_conn(cfg)
        data = _project_detail_data(
            conn, cfg, _effective_cfg(cfg), project_id, _match_filters_from_request()
        )
        if data is None:
            abort(404)
        return render_template("_project_detail_live.html", **data)

    @app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
    def project_edit(project_id):
        conn = _get_conn(cfg)
        project = db.get_project(conn, project_id)
        if project is None:
            abort(404)
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Project name is required.")
            else:
                source_names = _effective_cfg(cfg).sources.enabled_names()
                proj_sources = _selected_sources(request.form, source_names)
                db.update_project(conn, project_id, name, proj_sources)
                flash("Project updated.")
                return redirect(url_for("project_detail", project_id=project_id))
        raw_sources = project["sources"]
        return render_template(
            "project_form.html",
            project=project,
            project_sources=json.loads(raw_sources) if raw_sources else None,
            form=request.form if request.method == "POST" else None,
        )

    @app.route("/projects/<int:project_id>/archive", methods=["POST"])
    def project_archive(project_id):
        conn = _get_conn(cfg)
        project = db.get_project(conn, project_id)
        if project is None:
            abort(404)
        db.set_project_archived(conn, project_id, not project["archived"])
        flash(("Unarchived" if project["archived"] else "Archived") + f" '{project['name']}'.")
        return redirect(url_for("projects"))

    @app.route("/projects/<int:project_id>/delete", methods=["POST"])
    def project_delete(project_id):
        conn = _get_conn(cfg)
        project = db.get_project(conn, project_id)
        if project is None:
            abort(404)
        db.delete_project(conn, project_id)
        flash(f"Deleted project '{project['name']}' and its items.")
        return redirect(url_for("projects"))

    @app.route("/import-config", methods=["POST"])
    def import_config():
        count = db.import_config(_get_conn(cfg), cfg)
        flash(f"Imported {count} item(s) from YAML config.")
        return redirect(url_for("projects"))

    # --- Project JSON/YAML import & export ----------------------------------

    def _import_raw_text() -> str:
        """Prefer an uploaded file's content; fall back to the pasted textarea."""
        upload = request.files.get("file")
        if upload and upload.filename:
            return upload.read().decode("utf-8", errors="replace")
        return request.form.get("payload") or request.form.get("raw_text") or ""

    @app.route("/projects/import", methods=["GET", "POST"])
    def project_import_form():
        if request.method == "GET":
            return render_template("project_import.html", plan=None, raw_text="", dry_run_checked=False)
        conn = _get_conn(cfg)
        raw_text = _import_raw_text()
        dry_run_checked = bool(request.form.get("dry_run"))
        plan = project_import.build_plan(
            conn, _effective_cfg(cfg), raw_text, dry_run_override=True if dry_run_checked else None
        )
        return render_template(
            "project_import.html", plan=plan, raw_text=raw_text, dry_run_checked=dry_run_checked
        )

    @app.route("/projects/import/commit", methods=["POST"])
    def project_import_commit():
        conn = _get_conn(cfg)
        raw_text = request.form.get("raw_text") or ""
        dry_run_checked = bool(request.form.get("dry_run"))
        # Re-validate against current database state rather than trusting the
        # plan implied by an earlier preview render — the target project (or
        # an item within it) may have changed since then.
        plan = project_import.build_plan(
            conn, _effective_cfg(cfg), raw_text, dry_run_override=True if dry_run_checked else None
        )
        if not plan.valid:
            flash("Import could not be validated — see errors below.")
            return render_template(
                "project_import.html", plan=plan, raw_text=raw_text, dry_run_checked=dry_run_checked
            )
        result = project_import.apply_plan(conn, plan)
        if result.dry_run:
            flash(
                f"Dry run complete — {len(result.created_items)} item(s) would be created, "
                f"{len(result.updated_items)} would be updated. Nothing was written."
            )
            return render_template(
                "project_import.html", plan=plan, raw_text=raw_text,
                dry_run_checked=dry_run_checked, result=result,
            )
        flash(
            f"Imported into '{plan.project_name}': {len(result.created_items)} item(s) created, "
            f"{len(result.updated_items)} updated."
        )
        return redirect(url_for("project_detail", project_id=result.project_id))

    @app.route("/projects/<int:project_id>/export")
    def project_export(project_id):
        conn = _get_conn(cfg)
        project = db.get_project(conn, project_id)
        if project is None:
            abort(404)
        doc = project_import.export_project(conn, project_id)
        fmt = request.args.get("format", "yaml")
        if fmt == "json":
            body, mimetype, ext = project_import.to_json(doc), "application/json", "json"
        else:
            body, mimetype, ext = project_import.to_yaml(doc), "application/x-yaml", "yaml"
        response = app.response_class(body, mimetype=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="{project["slug"]}.{ext}"'
        return response

    # --- Items (managed inline on the project detail page) -----------------------

    @app.route("/items/new", methods=["GET", "POST"])
    def item_new():
        conn = _get_conn(cfg)
        if request.method == "POST":
            project_id = request.form.get("project_id", type=int)
        else:
            project_id = request.args.get("project_id", type=int)
        project = db.get_project(conn, project_id) if project_id else None
        if project is None:
            flash("Choose a project first.")
            return redirect(url_for("projects"))
        if request.method == "POST":
            item, errors = _item_from_form(request.form, _effective_cfg(cfg).sources.enabled_names())
            for error in errors:
                flash(error)
            if not errors:
                db.create_item(conn, project_id, item)
                flash(f"Item '{item.name}' created.")
                return redirect(url_for("project_detail", project_id=project_id))
        return render_template(
            "item_form.html",
            item=None,
            project=project,
            form=request.form,
        )

    @app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
    def item_edit(item_id):
        conn = _get_conn(cfg)
        row = db.get_item(conn, item_id)
        if row is None:
            abort(404)
        if request.method == "POST":
            item, errors = _item_from_form(request.form, _effective_cfg(cfg).sources.enabled_names())
            for error in errors:
                flash(error)
            if not errors:
                db.update_item(conn, item_id, item)
                result = runner.reassess_item_matches(conn, item_id, item)
                message = "Item updated."
                if result["rescored"] or result["excluded"]:
                    message += (
                        f" Re-scored {result['rescored']} existing match(es), "
                        f"removed {result['excluded']} now-excluded."
                    )
                flash(message)
                return redirect(url_for("project_detail", project_id=row["project_id"]))
        return render_template(
            "item_form.html",
            item=row,
            item_cfg=db._item_from_row(row),
            project=db.get_project(conn, row["project_id"]),
            products=db.list_products(conn, item_id),
            suggestions=db.list_product_suggestions(conn, item_id),
            auto_approve_threshold=db.get_auto_approve_threshold(conn),
            form=request.form,
        )

    @app.route("/items/<int:item_id>/archive", methods=["POST"])
    def item_archive(item_id):
        conn = _get_conn(cfg)
        row = db.get_item(conn, item_id)
        if row is None:
            abort(404)
        db.set_item_archived(conn, item_id, not row["archived"])
        flash(("Unarchived" if row["archived"] else "Archived") + f" '{row['name']}'.")
        return redirect(url_for("project_detail", project_id=row["project_id"]))

    @app.route("/items/<int:item_id>/delete", methods=["POST"])
    def item_delete(item_id):
        conn = _get_conn(cfg)
        row = db.get_item(conn, item_id)
        if row is None:
            abort(404)
        db.delete_item(conn, item_id)
        flash(f"Deleted item '{row['name']}'.")
        return redirect(url_for("project_detail", project_id=row["project_id"]))

    # --- Product catalogue (managed inline on the item edit page) -----------------

    @app.route("/items/<int:item_id>/products/new", methods=["GET", "POST"])
    def product_new(item_id):
        conn = _get_conn(cfg)
        item = db.get_item(conn, item_id)
        if item is None:
            abort(404)
        if request.method == "POST":
            fields, errors = _product_from_form(request.form)
            for error in errors:
                flash(error)
            if not errors:
                db.create_product(conn, item_id, **fields)
                flash(f"Product '{fields['manufacturer']}' added.")
                return redirect(url_for("item_edit", item_id=item_id))
        return render_template("product_form.html", product=None, item=item, form=request.form)

    # NOTE: product_id below identifies an item_products row (this item's own
    # tracking of a catalogue product), not the global products row — see
    # docs/adr/0007-catalogue-globalization.md. Editing manufacturer/model/
    # msrp/typical_new_price here still edits the shared global product
    # (affects every item tracking it); match_terms/target_deal_price/
    # archived/wanted are this item's own. Retailer price-candidate routes
    # further down key on the *global* product id instead.
    @app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
    def product_edit(product_id):
        conn = _get_conn(cfg)
        row = db.get_item_product_by_id(conn, product_id)
        if row is None:
            abort(404)
        item = db.get_item(conn, row["item_id"])
        if request.method == "POST":
            fields, errors = _product_from_form(request.form)
            for error in errors:
                flash(error)
            if not errors:
                db.update_product(conn, product_id, **fields)
                flash("Product updated.")
                return redirect(url_for("item_edit", item_id=row["item_id"]))
        return render_template(
            "product_form.html",
            product=row,
            item=item,
            form=request.form,
            price_candidates=db.list_price_candidates(conn, row["product_id"]),
            searxng_enabled=cfg.searxng.enabled,
            price_refresh_interval_hours=cfg.searxng.refresh_interval_hours,
        )

    @app.route("/products/<int:product_id>/archive", methods=["POST"])
    def product_archive(product_id):
        conn = _get_conn(cfg)
        row = db.get_item_product_by_id(conn, product_id)
        if row is None:
            abort(404)
        db.set_product_archived(conn, product_id, not row["archived"])
        flash(("Unarchived" if row["archived"] else "Archived") + f" '{row['manufacturer']}'.")
        return redirect(url_for("item_edit", item_id=row["item_id"]))

    @app.route("/products/<int:product_id>/delete", methods=["POST"])
    def product_delete(product_id):
        conn = _get_conn(cfg)
        row = db.get_item_product_by_id(conn, product_id)
        if row is None:
            abort(404)
        # Stops this item tracking the product; the shared catalogue entry
        # and its price history are kept (see db.delete_item_product).
        db.delete_item_product(conn, product_id)
        flash(f"Removed '{row['manufacturer']}' from this item's catalogue.")
        return redirect(url_for("item_edit", item_id=row["item_id"]))

    # --- Retailer price discovery (see retailer_price.py) -------------------------
    #
    # These key on the *global* product id (product_price_candidates and the
    # canonical retailer URL are shared platform data, not per-item).

    @app.route("/products/<int:product_id>/price-candidates/search", methods=["POST"])
    def price_candidates_search(product_id):
        conn = _get_conn(cfg)
        item_product = db.get_item_product_by_id(conn, product_id)
        if item_product is None:
            abort(404)
        global_id = item_product["product_id"]
        row = db.get_product(conn, global_id)
        if not cfg.searxng.enabled:
            flash("Retailer price discovery is disabled (set searxng.enabled in config.yaml).")
            return redirect(url_for("product_edit", product_id=product_id))
        candidates = retailer_price.search_candidates(row["manufacturer"], row["model"] or "", cfg.searxng)
        db.record_price_candidates(conn, global_id, candidates)
        flash(f"Found {len(candidates)} retailer price candidate(s)." if candidates
              else "No retailer price candidates found.")
        return redirect(url_for("product_edit", product_id=product_id))

    @app.route("/price-candidates/<int:candidate_id>/approve", methods=["POST"])
    def price_candidate_approve(candidate_id):
        conn = _get_conn(cfg)
        candidate = db.get_price_candidate(conn, candidate_id)
        if candidate is None:
            abort(404)
        refreshed = retailer_price.fetch_price(candidate["url"], cfg.searxng.timeout)
        db.approve_price_candidate(conn, candidate_id, refreshed)
        flash(f"Retailer price set from {candidate['domain']}.")
        # The candidate only carries the global product id — resolve back to
        # *an* item_products row tracking it so the edit-page redirect still
        # resolves (any item tracking this product shows the same result).
        item_product = conn.execute(
            "SELECT id FROM item_products WHERE product_id = ? LIMIT 1", (candidate["product_id"],)
        ).fetchone()
        if item_product is None:
            return redirect(url_for("catalogue_review"))
        return redirect(url_for("product_edit", product_id=item_product["id"]))

    @app.route("/products/<int:product_id>/price-candidates/dismiss", methods=["POST"])
    def price_candidates_dismiss(product_id):
        conn = _get_conn(cfg)
        item_product = db.get_item_product_by_id(conn, product_id)
        if item_product is None:
            abort(404)
        db.clear_price_candidates(conn, item_product["product_id"])
        flash("Price candidates dismissed.")
        return redirect(url_for("product_edit", product_id=product_id))

    # --- Product suggestions (spotted automatically, awaiting review) -------------

    def _suggestion_redirect(item_id):
        # Same actions are posted from both the per-item form and the global
        # /catalogue review page — a relative `next` field says where to
        # return to (relative-only, so it can't redirect off-host).
        next_url = request.form.get("next", "")
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("item_edit", item_id=item_id) if item_id else url_for("projects"))

    @app.route("/catalogue")
    def catalogue_review():
        conn = _get_conn(cfg)
        suggestions = db.triage_pending_suggestions(conn)
        verdict_counts = {}
        for s in suggestions:
            verdict_counts[s["verdict"]] = verdict_counts.get(s["verdict"], 0) + 1
        return render_template(
            "catalogue.html",
            suggestions=suggestions,
            verdict_counts=verdict_counts,
            suspects=db.find_suspect_products(conn),
            auto_approve_threshold=db.get_auto_approve_threshold(conn),
            last_action=db.get_last_suggestion_action(conn),
        )

    @app.route("/suggestions/undo", methods=["POST"])
    def suggestion_undo():
        conn = _get_conn(cfg)
        message = db.undo_last_suggestion_action(conn)
        flash(f"Undone: {message}" if message else "Nothing to undo.")
        return _suggestion_redirect(None)

    @app.route("/products/bulk-archive", methods=["POST"])
    def product_bulk_archive():
        conn = _get_conn(cfg)
        count = 0
        for product_id in request.form.getlist("product_ids", type=int):
            if db.get_item_product_by_id(conn, product_id) is not None:
                db.set_product_archived(conn, product_id, True)
                count += 1
        flash(
            f"Archived {count} product(s) — they stop matching immediately and "
            "existing matches un-verify on each listing's next rescan."
            if count else "No products selected."
        )
        return _suggestion_redirect(None)

    @app.route("/products/bulk-knowledge-only", methods=["POST"])
    def product_bulk_knowledge_only():
        conn = _get_conn(cfg)
        count = 0
        for product_id in request.form.getlist("product_ids", type=int):
            if db.get_item_product_by_id(conn, product_id) is not None:
                db.set_product_wanted(conn, product_id, False)
                count += 1
        flash(
            f"Marked {count} product(s) knowledge-only — still tracked and priced, "
            "no longer surfaced as deals."
            if count else "No products selected."
        )
        return _suggestion_redirect(None)

    @app.route("/products/<int:product_id>/toggle-wanted", methods=["POST"])
    def product_toggle_wanted(product_id):
        conn = _get_conn(cfg)
        row = db.get_item_product_by_id(conn, product_id)
        if row is None:
            abort(404)
        db.set_product_wanted(conn, product_id, not row["wanted"])
        flash(
            f"'{row['manufacturer']} {row['model']}'".strip()
            + (" back on deal surfaces." if not row["wanted"] else " is now knowledge-only.")
        )
        return _suggestion_redirect(None) if request.form.get("next") else redirect(
            url_for("item_edit", item_id=row["item_id"])
        )

    @app.route("/suggestions/<int:suggestion_id>/approve", methods=["POST"])
    def suggestion_approve(suggestion_id):
        conn = _get_conn(cfg)
        suggestion = db.get_product_suggestion(conn, suggestion_id)
        if suggestion is None:
            abort(404)
        # Optional model correction at approval time (e.g. seller field held
        # an article number, human knows the real model name).
        model = (request.form.get("model") or "").strip()
        product_id = db.approve_suggestion(conn, suggestion_id, model=model or None)
        label = f"{suggestion['manufacturer']} {model or suggestion['model']}".strip()
        item = db.get_item(conn, suggestion["item_id"])
        item_product = db.get_item_product(conn, suggestion["item_id"], product_id)
        db.record_last_suggestion_action(conn, {
            "kind": "approve",
            "suggestion_id": suggestion_id,
            "item_product_id": item_product["id"] if item_product else None,
            "description": f'approved "{label}" for {item["name"] if item else "an item"}',
        })
        flash(f"Added '{label}' to the catalogue.")
        return _suggestion_redirect(suggestion["item_id"])

    @app.route("/suggestions/<int:suggestion_id>/dismiss", methods=["POST"])
    def suggestion_dismiss(suggestion_id):
        conn = _get_conn(cfg)
        suggestion = db.get_product_suggestion(conn, suggestion_id)
        if suggestion is None:
            abort(404)
        db.dismiss_suggestion(conn, suggestion_id)
        label = f"{suggestion['manufacturer']} {suggestion['model']}".strip()
        item = db.get_item(conn, suggestion["item_id"])
        db.record_last_suggestion_action(conn, {
            "kind": "dismiss",
            "suggestion_id": suggestion_id,
            "description": f'dismissed "{label}" for {item["name"] if item else "an item"}',
        })
        flash("Suggestion dismissed.")
        return _suggestion_redirect(suggestion["item_id"])

    @app.route("/suggestions/bulk-approve", methods=["POST"])
    def suggestion_bulk_approve():
        conn = _get_conn(cfg)
        item_id = request.form.get("item_id", type=int)
        count = skipped = 0
        undo_items = []
        for suggestion_id in request.form.getlist("suggestion_ids", type=int):
            suggestion = db.get_product_suggestion(conn, suggestion_id)
            if suggestion is None or suggestion["status"] != "pending":
                continue
            # A brand with no model would become a product whose match term
            # is the bare brand name — matching every listing of that brand
            # against one reference price, which defeats the catalogue's
            # model-level pricing. Approving one is still allowed, but only
            # as a deliberate individual click, never as part of a sweep.
            if not suggestion["model"]:
                skipped += 1
                continue
            product_id = db.approve_suggestion(conn, suggestion_id)
            item_product = db.get_item_product(conn, suggestion["item_id"], product_id)
            undo_items.append({
                "suggestion_id": suggestion_id,
                "item_product_id": item_product["id"] if item_product else None,
            })
            count += 1
        message = f"Approved {count} suggestion(s)." if count else "No suggestions selected."
        if skipped:
            message += (
                f" Skipped {skipped} brand-only suggestion(s) — a bare brand makes a"
                " poor product; approve individually if you really want it."
            )
        if undo_items:
            db.record_last_suggestion_action(conn, {
                "kind": "bulk_approve",
                "items": undo_items,
                "description": f"approved {len(undo_items)} suggestion(s)",
            })
        flash(message)
        return _suggestion_redirect(item_id)

    @app.route("/suggestions/bulk-dismiss", methods=["POST"])
    def suggestion_bulk_dismiss():
        conn = _get_conn(cfg)
        item_id = request.form.get("item_id", type=int)
        dismissed_ids = []
        for suggestion_id in request.form.getlist("suggestion_ids", type=int):
            suggestion = db.get_product_suggestion(conn, suggestion_id)
            if suggestion is not None and suggestion["status"] == "pending":
                db.dismiss_suggestion(conn, suggestion_id)
                dismissed_ids.append(suggestion_id)
        if dismissed_ids:
            db.record_last_suggestion_action(conn, {
                "kind": "bulk_dismiss",
                "suggestion_ids": dismissed_ids,
                "description": f"dismissed {len(dismissed_ids)} suggestion(s)",
            })
        flash(f"Dismissed {len(dismissed_ids)} suggestion(s)." if dismissed_ids else "No suggestions selected.")
        return _suggestion_redirect(item_id)

    @app.route("/catalogue-settings", methods=["POST"])
    def catalogue_settings():
        conn = _get_conn(cfg)
        raw = (request.form.get("auto_approve_threshold") or "").strip()
        if raw:
            try:
                value = max(0.0, min(100.0, float(raw)))
            except ValueError:
                flash("Auto-approve threshold must be a number.")
                return redirect(request.referrer or url_for("projects"))
        else:
            value = None
        db.set_auto_approve_threshold(conn, value)
        flash("Catalogue suggestion settings updated.")
        return redirect(request.referrer or url_for("projects"))

    # --- Duplicate listings (spotted automatically, awaiting review) ---------------

    def _duplicate_redirect(dup):
        return redirect(
            url_for("project_detail", project_id=dup["project_id"]) + "#duplicates"
        )

    @app.route("/duplicates/<int:dup_id>/confirm", methods=["POST"])
    def duplicate_confirm(dup_id):
        conn = _get_conn(cfg)
        dup = db.get_duplicate(conn, dup_id)
        if dup is None:
            abort(404)
        kept_listing_id = request.form.get("kept_listing_id", type=int)
        try:
            db.confirm_duplicate(conn, dup_id, kept_listing_id)
        except ValueError:
            flash("That pair has already been decided.")
            return _duplicate_redirect(dup)
        flash("Confirmed as the same item — the other listing is hidden now.")
        return _duplicate_redirect(dup)

    @app.route("/duplicates/<int:dup_id>/dismiss", methods=["POST"])
    def duplicate_dismiss(dup_id):
        conn = _get_conn(cfg)
        dup = db.get_duplicate(conn, dup_id)
        if dup is None:
            abort(404)
        db.dismiss_duplicate(conn, dup_id)
        flash("Marked as different items — this pair won't be suggested again.")
        return _duplicate_redirect(dup)

    @app.route("/duplicates/<int:dup_id>/revert", methods=["POST"])
    def duplicate_revert(dup_id):
        conn = _get_conn(cfg)
        dup = db.get_duplicate(conn, dup_id)
        if dup is None:
            abort(404)
        db.revert_duplicate(conn, dup_id)
        flash("Decision undone — the pair is awaiting review again.")
        return _duplicate_redirect(dup)

    @app.route("/duplicates/bulk-confirm", methods=["POST"])
    def duplicate_bulk_confirm():
        # Auto-picks which listing to keep (the cheaper live one) — see
        # db.confirm_duplicate(kept_listing_id=None).
        conn = _get_conn(cfg)
        project_id = request.form.get("project_id", type=int)
        count = 0
        for dup_id in request.form.getlist("dup_ids", type=int):
            dup = db.get_duplicate(conn, dup_id)
            if dup is not None and dup["status"] == "pending":
                db.confirm_duplicate(conn, dup_id)
                count += 1
        flash(f"Confirmed {count} pair(s), keeping the cheaper listing of each."
              if count else "No pairs selected.")
        target = url_for("project_detail", project_id=project_id) + "#duplicates" \
            if project_id else url_for("dashboard")
        return redirect(target)

    @app.route("/duplicates/bulk-dismiss", methods=["POST"])
    def duplicate_bulk_dismiss():
        conn = _get_conn(cfg)
        project_id = request.form.get("project_id", type=int)
        count = 0
        for dup_id in request.form.getlist("dup_ids", type=int):
            dup = db.get_duplicate(conn, dup_id)
            if dup is not None and dup["status"] == "pending":
                db.dismiss_duplicate(conn, dup_id)
                count += 1
        flash(f"Dismissed {count} pair(s)." if count else "No pairs selected.")
        target = url_for("project_detail", project_id=project_id) + "#duplicates" \
            if project_id else url_for("dashboard")
        return redirect(target)

    # --- Manual searches ----------------------------------------------------------

    @app.route("/manual")
    def manual():
        conn = _get_conn(cfg)
        eff_cfg = _effective_cfg(cfg)
        registry = sources.build_registry(eff_cfg)
        groups = []
        for project in db.load_project_configs(conn):
            for item in project.items:
                links = []
                for name in runner.item_sources(item, eff_cfg, project):
                    source = registry.get(name)
                    if source is not None and not source.is_automated():
                        links.extend(source.manual_links(item))
                if links:
                    groups.append((project.name, item.name, links))
        return render_template("manual.html", groups=groups)

    return app
