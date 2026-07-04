"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import auction_watch, db, runner, sources
from .config import AppConfig, ConfigError, load_config

log = logging.getLogger("product_finder")

# How often `watch` checks for auctions nearing their close, independent of
# the (much coarser) full search interval — see auction_watch.py for the
# tiered per-auction cadence this drives.
_AUCTION_POLL_SECONDS = 20


def _print_run_summary(cfg: AppConfig, projects: list, new_alerts: list) -> None:
    if new_alerts:
        print(f"\n{len(new_alerts)} new match(es) found.")
    else:
        print("No new matches this run.")
    registry = sources.build_registry(cfg)
    manual = runner.collect_manual_links(cfg, projects, registry)
    if manual:
        automated = [n for n, s in registry.items() if s.is_automated()]
        skipped = sorted({l.source for l in manual})
        print(f"Automated sources: {', '.join(automated) or 'none'}")
        print(f"Manual-assisted sources ({', '.join(skipped)}): "
              f"{len(manual)} search link(s) — see the web UI's Manual searches page.")


def cmd_run_once(cfg: AppConfig) -> int:
    conn = db.connect(cfg.db_path)
    try:
        new_alerts = runner.run_once(cfg, conn)
        cfg = db.effective_config(conn, cfg)  # for the summary's source list
        projects = db.load_project_configs(conn)
    finally:
        conn.close()
    _print_run_summary(cfg, projects, new_alerts)
    return 0


def cmd_watch(cfg: AppConfig) -> int:
    interval_seconds = max(1, cfg.interval_minutes) * 60
    print(
        f"Watching every {cfg.interval_minutes} minute(s) "
        f"(auction closes checked every {_AUCTION_POLL_SECONDS}s). Ctrl-C to stop."
    )
    next_full_run = 0.0  # due immediately on first tick
    while True:
        conn = db.connect(cfg.db_path)
        try:
            now = time.monotonic()
            if now >= next_full_run:
                new_alerts = runner.run_once(cfg, conn)
                run_cfg = db.effective_config(conn, cfg)  # picks up live Sources-page edits
                projects = db.load_project_configs(conn)
                _print_run_summary(run_cfg, projects, new_alerts)
                next_full_run = now + interval_seconds
            captured = auction_watch.poll_and_capture(cfg, conn)
            if captured:
                print(f"Captured closing price for {captured} ended auction(s).")
        except Exception as exc:
            log.error("Watch cycle failed: %s", exc)
        finally:
            conn.close()
        try:
            time.sleep(_AUCTION_POLL_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def cmd_import_config(cfg: AppConfig) -> int:
    conn = db.connect(cfg.db_path)
    try:
        count = db.import_config(conn, cfg)
    finally:
        conn.close()
    print(f"Imported {len(cfg.projects)} project(s), {count} item(s) from YAML config.")
    return 0


def cmd_list_projects(cfg: AppConfig) -> int:
    conn = db.connect(cfg.db_path)
    try:
        projects = runner.load_projects(cfg, conn)
    finally:
        conn.close()
    for project in projects:
        print(f"{project.slug}: {project.name} ({len(project.items)} item(s))")
    if not projects:
        print("No projects configured.")
    return 0


def cmd_list_items(cfg: AppConfig) -> int:
    conn = db.connect(cfg.db_path)
    try:
        projects = runner.load_projects(cfg, conn)
    finally:
        conn.close()
    for project in projects:
        print(f"{project.name}:")
        for item in project.items:
            bits = [f"max £{item.max_price:g}" if item.max_price else "no max"]
            if item.normal_price:
                bits.append(f"normal £{item.normal_price:g}")
            if item.target_deal_price:
                bits.append(f"target £{item.target_deal_price:g}")
            bits.append(f"priority {item.priority}")
            print(f"  - {item.name} ({', '.join(bits)})")
            print(f"    terms: {', '.join(item.terms)}")
    if not projects:
        print("No projects configured.")
    return 0


def cmd_catalogue_tidy(cfg: AppConfig) -> int:
    """One-shot catalogue maintenance, safe to re-run: replay pending
    suggestions through the current normalisation rules (casing variants
    merge, junk placeholders drop out), then fold exact-duplicate products
    into their oldest copy. Decided suggestions and distinct products are
    never touched."""
    conn = db.connect(cfg.db_path)
    try:
        result = db.renormalize_pending_suggestions(conn)
        merged = db.dedupe_products(conn)
    finally:
        conn.close()
    print(
        f"Suggestions: {result['before']} pending -> {result['after']} "
        f"({result['rejected_outright']} rejected as junk, "
        f"{result['before'] - result['after'] - result['rejected_outright']} merged)."
    )
    print(f"Products: {merged} exact duplicate(s) folded away.")
    return 0


def cmd_web(cfg: AppConfig, port: int) -> int:
    from .web.app import create_app

    app = create_app(cfg)
    print(f"Product Finder UI: http://127.0.0.1:{port} (Ctrl-C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="product-finder",
        description="Track wanted products across second-hand marketplaces.",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to YAML config")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-once", help="Run one search cycle, alert on new matches")
    sub.add_parser("watch", help="Run continuously at the configured interval")
    sub.add_parser("import-config", help="Import/merge YAML projects and items into the database")
    sub.add_parser("list-projects", help="List projects")
    sub.add_parser("list-items", help="List items per project")
    sub.add_parser(
        "catalogue-tidy",
        help="Re-normalise pending suggestions and merge exact-duplicate products (idempotent)",
    )
    web = sub.add_parser("web", help="Run the local web UI (localhost only)")
    web.add_argument("-p", "--port", type=int, default=8765, help="Port (default 8765)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "web":
        return cmd_web(cfg, args.port)

    commands = {
        "run-once": cmd_run_once,
        "watch": cmd_watch,
        "import-config": cmd_import_config,
        "list-projects": cmd_list_projects,
        "list-items": cmd_list_items,
        "catalogue-tidy": cmd_catalogue_tidy,
    }
    return commands[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
