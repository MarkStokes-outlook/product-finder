"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import db, runner, sources
from .alerts import markdown as markdown_report
from .config import AppConfig, ConfigError, load_config

log = logging.getLogger("product_finder")


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
              f"{len(manual)} search links in the report.")
    if cfg.alerts.markdown_report:
        print(f"Report: {cfg.report_path}")


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
    interval = max(1, cfg.interval_minutes)
    print(f"Watching every {interval} minute(s). Ctrl-C to stop.")
    while True:
        conn = db.connect(cfg.db_path)
        try:
            new_alerts = runner.run_once(cfg, conn)
            run_cfg = db.effective_config(conn, cfg)  # picks up live Sources-page edits
            projects = db.load_project_configs(conn)
            _print_run_summary(run_cfg, projects, new_alerts)
        except Exception as exc:
            log.error("Run failed: %s", exc)
        finally:
            conn.close()
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def cmd_report(cfg: AppConfig) -> int:
    conn = db.connect(cfg.db_path)
    try:
        cfg = db.effective_config(conn, cfg)
        projects = runner.load_projects(cfg, conn)
        path = markdown_report.write_report(
            conn, cfg, runner.collect_manual_links(cfg, projects)
        )
    finally:
        conn.close()
    print(f"Report written to {path}")
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
    sub.add_parser("report", help="Regenerate the Markdown report from stored data")
    sub.add_parser("import-config", help="Import/merge YAML projects and items into the database")
    sub.add_parser("list-projects", help="List projects")
    sub.add_parser("list-items", help="List items per project")
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
        "report": cmd_report,
        "import-config": cmd_import_config,
        "list-projects": cmd_list_projects,
        "list-items": cmd_list_items,
    }
    return commands[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
