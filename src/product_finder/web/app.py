"""Local web UI. Flask, server-rendered, localhost only, no auth by design."""

from __future__ import annotations

import json
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .. import db, runner, sources
from ..config import AppConfig, ItemConfig


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
    selected = [s for s in source_names if form.get(f"source_{s}")]
    item_sources = selected if selected and set(selected) != set(source_names) else None

    item = ItemConfig(
        name=name,
        terms=terms,
        max_price=parse_price("max_price"),
        normal_price=parse_price("normal_price"),
        target_deal_price=parse_price("target_deal_price"),
        priority=priority,
        notes=(form.get("notes") or "").strip(),
        exclude_terms=exclude_terms,
        sources=item_sources,
    )
    return (item if not errors else None), errors


def _reports_mtime(cfg: AppConfig) -> float | None:
    """Report file mtime — rewritten on every `run_once`, whether or not it
    found new matches, so it's a cheap "did a run just happen" signal for
    the dashboard's polling."""
    path = Path(cfg.report_path)
    return path.stat().st_mtime if path.exists() else None


def _dashboard_data(conn, cfg: AppConfig) -> dict:
    return {
        "summaries": db.project_summaries(conn),
        "best": db.query_matches(conn, flagged=False, sort="score", limit=10),
        "warnings": db.query_matches(conn, flagged=True, sort="score", limit=10),
        "reports": {"md": Path(cfg.report_path).exists()},
    }


def _project_detail_data(conn, cfg: AppConfig, eff_cfg: AppConfig, project_id: int) -> dict | None:
    project = db.get_project(conn, project_id)
    if project is None:
        return None
    items = db.list_items(conn, project_id=project_id, include_archived=False)
    rows = db.project_detail_matches(conn, project_id)
    matches_by_item: dict[int, list] = {}
    for row in rows:
        matches_by_item.setdefault(row["item_id"], []).append(row)
    project_cfg = next(
        (p for p in db.load_project_configs(conn) if p.id == project_id), None
    )
    manual_links = runner.collect_manual_links(eff_cfg, [project_cfg]) if project_cfg else []
    return {
        "project": project,
        "items": items,
        "matches_by_item": matches_by_item,
        "manual_links": manual_links,
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

    # --- Dashboard -----------------------------------------------------------

    @app.route("/")
    def dashboard():
        conn = _get_conn(cfg)
        return render_template(
            "dashboard.html",
            reports_mtime=_reports_mtime(cfg),
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
        return {"reports_mtime": _reports_mtime(cfg)}

    @app.route("/reports/<kind>")
    def report_file(kind):
        if kind != "md":
            abort(404)
        path = Path(cfg.report_path)
        if not path.exists():
            abort(404)
        return send_file(path.resolve(), mimetype="text/plain")

    # --- Sources ---------------------------------------------------------------

    @app.route("/sources")
    def source_list():
        eff_cfg = _effective_cfg(cfg)
        sc = eff_cfg.sources
        registry = sources.build_registry(eff_cfg)
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
                db.create_project(_get_conn(cfg), name)
                flash(f"Project '{name}' created.")
                return redirect(url_for("projects"))
        return render_template("project_form.html", project=None)

    @app.route("/projects/<int:project_id>")
    def project_detail(project_id):
        conn = _get_conn(cfg)
        data = _project_detail_data(conn, cfg, _effective_cfg(cfg), project_id)
        if data is None:
            abort(404)
        return render_template(
            "project_detail.html", reports_mtime=_reports_mtime(cfg), **data
        )

    @app.route("/projects/<int:project_id>/live")
    def project_detail_live(project_id):
        # Fragment-only render for the polling JS to swap in — same pattern
        # as /dashboard/live.
        conn = _get_conn(cfg)
        data = _project_detail_data(conn, cfg, _effective_cfg(cfg), project_id)
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
                db.update_project(conn, project_id, name)
                flash("Project updated.")
                return redirect(url_for("projects"))
        return render_template("project_form.html", project=project)

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

    # --- Items ----------------------------------------------------------------

    @app.route("/items")
    def items():
        conn = _get_conn(cfg)
        project_id = request.args.get("project_id", type=int)
        return render_template(
            "items.html",
            items=db.list_items(conn, project_id=project_id),
            projects=db.list_projects(conn),
            selected_project=project_id,
            json=json,
        )

    @app.route("/items/new", methods=["GET", "POST"])
    def item_new():
        conn = _get_conn(cfg)
        projects_list = db.list_projects(conn)
        if not projects_list:
            flash("Create a project first.")
            return redirect(url_for("project_new"))
        if request.method == "POST":
            project_id = request.form.get("project_id", type=int)
            item, errors = _item_from_form(request.form, _effective_cfg(cfg).sources.enabled_names())
            if project_id is None or db.get_project(conn, project_id) is None:
                errors.append("Choose a valid project.")
            for error in errors:
                flash(error)
            if not errors:
                db.create_item(conn, project_id, item)
                flash(f"Item '{item.name}' created.")
                return redirect(url_for("items", project_id=project_id))
        return render_template(
            "item_form.html",
            item=None,
            projects=projects_list,
            selected_project=request.args.get("project_id", type=int),
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
                flash("Item updated.")
                return redirect(url_for("items", project_id=row["project_id"]))
        return render_template(
            "item_form.html",
            item=row,
            item_cfg=db._item_from_row(row),
            projects=db.list_projects(conn),
            selected_project=row["project_id"],
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
        return redirect(url_for("items", project_id=row["project_id"]))

    @app.route("/items/<int:item_id>/delete", methods=["POST"])
    def item_delete(item_id):
        conn = _get_conn(cfg)
        row = db.get_item(conn, item_id)
        if row is None:
            abort(404)
        db.delete_item(conn, item_id)
        flash(f"Deleted item '{row['name']}'.")
        return redirect(url_for("items", project_id=row["project_id"]))

    # --- Listings ---------------------------------------------------------------

    @app.route("/listings")
    def listings():
        conn = _get_conn(cfg)
        project_id = request.args.get("project_id", type=int)
        item_id = request.args.get("item_id", type=int)
        source = request.args.get("source") or None
        grade = request.args.get("grade") or None
        flagged_raw = request.args.get("flagged", "")
        flagged = {"yes": True, "no": False}.get(flagged_raw)
        sort = request.args.get("sort", "score")
        rows = db.query_matches(
            conn,
            project_id=project_id,
            item_id=item_id,
            source=source,
            grade=grade,
            flagged=flagged,
            sort=sort,
            limit=500,
        )
        return render_template(
            "listings.html",
            rows=rows,
            projects=db.list_projects(conn),
            items=db.list_items(conn, project_id=project_id),
            filters={
                "project_id": project_id,
                "item_id": item_id,
                "source": source or "",
                "grade": grade or "",
                "flagged": flagged_raw,
                "sort": sort,
            },
            json=json,
        )

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
                for name in runner.item_sources(item, eff_cfg):
                    source = registry.get(name)
                    if source is not None and not source.is_automated():
                        links.extend(source.manual_links(item))
                if links:
                    groups.append((project.name, item.name, links))
        return render_template("manual.html", groups=groups)

    return app
