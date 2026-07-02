"""Markdown report generation: reports/latest.md."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from ..config import AppConfig
from ..models import ManualLink


def _fmt_price(value, currency: str = "GBP") -> str:
    if value is None:
        return "—"
    symbol = "£" if currency == "GBP" else f"{currency} "
    return f"{symbol}{value:,.2f}".rstrip("0").rstrip(".")


def build_report(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    manual_links: list[ManualLink] | None = None,
) -> str:
    rows = db.report_rows(conn)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Product Finder — Deal Report",
        "",
        f"Generated: {now}",
        "",
    ]

    if not rows:
        lines += ["No matched listings yet. Run `python -m product_finder run-once`.", ""]

    current_project = None
    current_item = None
    for row in rows:
        if row["project_name"] != current_project:
            current_project = row["project_name"]
            current_item = None
            lines += [f"## {current_project}", ""]
        if row["item_name"] != current_item:
            current_item = row["item_name"]
            target = _fmt_price(row["target_deal_price"])
            normal = _fmt_price(row["normal_price"])
            lines += [
                f"### {current_item}",
                "",
                f"Normal price: {normal} · Target deal price: {target} · Priority: {row['priority']}",
                "",
                "| Score | Title | Price | Margin | % below | Grade | Flags | Source | First seen |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        flags = ", ".join(json.loads(row["flags"] or "[]")) or "—"
        under = " ✅" if row["under_target"] else ""
        title = row["title"].replace("|", "\\|")
        first_seen = (row["first_seen"] or "")[:10]
        lines.append(
            f"| {row['deal_score']:.0f} "
            f"| [{title}]({row['url']}) "
            f"| {_fmt_price(row['price'], row['currency'])}{under} "
            f"| {_fmt_price(row['margin_abs'])} "
            f"| {row['margin_pct']:.0f}% "
            f"| {row['grade']} "
            f"| {flags} "
            f"| {row['source']} "
            f"| {first_seen} |"
        )
        # blank line after each item table is added when item/project changes;
        # simpler to add trailing newline handling below
    lines.append("")

    if manual_links:
        lines += [
            "## Manual searches",
            "",
            "These sources are not automated — open the links to check manually:",
            "",
        ]
        for link in manual_links:
            lines.append(f"- [{link.label}]({link.url})")
        lines.append("")

    return "\n".join(lines)


def write_report(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    manual_links: list[ManualLink] | None = None,
) -> Path:
    path = Path(cfg.report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(conn, cfg, manual_links))
    return path
