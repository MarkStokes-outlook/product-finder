"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import db, runner, sources
from .alerts import html as html_report
from .alerts import markdown as markdown_report
from .config import AppConfig, ConfigError, load_config

log = logging.getLogger("product_finder")


def _print_run_summary(cfg: AppConfig, new_alerts: list) -> None:
    if new_alerts:
        print(f"\n{len(new_alerts)} new match(es) found.")
    else:
        print("No new matches this run.")
    manual = runner.collect_manual_links(cfg)
    if manual:
        automated = [n for n in cfg.sources.enabled_names() if sources.ALL[n].is_automated(cfg)]
        skipped = sorted({l.source for l in manual})
        print(f"Automated sources: {', '.join(automated) or 'none'}")
        print(f"Manual-assisted sources ({', '.join(skipped)}): "
              f"{len(manual)} search links in the report.")
    if cfg.alerts.markdown_report:
        print(f"Reports: {cfg.report_path} / {html_report.html_report_path(cfg)}")


def cmd_run_once(cfg: AppConfig) -> int:
    conn = db.connect(cfg.db_path)
    try:
        new_alerts = runner.run_once(cfg, conn)
    finally:
        conn.close()
    _print_run_summary(cfg, new_alerts)
    return 0


def cmd_watch(cfg: AppConfig) -> int:
    interval = max(1, cfg.interval_minutes)
    print(f"Watching every {interval} minute(s). Ctrl-C to stop.")
    while True:
        conn = db.connect(cfg.db_path)
        try:
            new_alerts = runner.run_once(cfg, conn)
            _print_run_summary(cfg, new_alerts)
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
        path = markdown_report.write_report(conn, cfg, runner.collect_manual_links(cfg))
    finally:
        conn.close()
    print(f"Report written to {path}")
    return 0


def cmd_report_html(cfg: AppConfig) -> int:
    conn = db.connect(cfg.db_path)
    try:
        path = html_report.write_html_report(conn, cfg, runner.collect_manual_links(cfg))
    finally:
        conn.close()
    print(f"HTML report written to {path}")
    return 0


def cmd_list_projects(cfg: AppConfig) -> int:
    for project in cfg.projects:
        print(f"{project.slug}: {project.name} ({len(project.items)} item(s))")
    if not cfg.projects:
        print("No projects configured.")
    return 0


def cmd_list_items(cfg: AppConfig) -> int:
    for project in cfg.projects:
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
    if not cfg.projects:
        print("No projects configured.")
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
    sub.add_parser("report-html", help="Regenerate the HTML report from stored data")
    sub.add_parser("list-projects", help="List configured projects")
    sub.add_parser("list-items", help="List configured items per project")
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

    commands = {
        "run-once": cmd_run_once,
        "watch": cmd_watch,
        "report": cmd_report,
        "report-html": cmd_report_html,
        "list-projects": cmd_list_projects,
        "list-items": cmd_list_items,
    }
    return commands[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
