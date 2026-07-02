"""Static HTML report generation: reports/latest.html.

Same data as the Markdown report, with just enough styling to scan deals
quickly. No server, no framework, no JavaScript.
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from ..config import AppConfig
from ..models import ManualLink

# Rows at or above this score (with no warning flags) are highlighted as
# excellent deals.
EXCELLENT_SCORE = 70

_STYLE = """
body { font-family: -apple-system, "Segoe UI", sans-serif; margin: 2rem auto;
       max-width: 72rem; padding: 0 1rem; color: #1a1a1a; }
h1 { border-bottom: 2px solid #ddd; padding-bottom: .3rem; }
h2 { margin-top: 2rem; }
h3 { margin-bottom: .2rem; }
p.meta { color: #666; margin-top: 0; font-size: .9rem; }
table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem;
        font-size: .9rem; }
th, td { border: 1px solid #ddd; padding: .4rem .6rem; text-align: left; }
th { background: #f5f5f5; }
tr.excellent { background: #e6f6e6; }
tr.warning { background: #fdecec; }
td.score { font-weight: bold; text-align: right; }
.badge { display: inline-block; padding: .05rem .45rem; border-radius: .6rem;
         font-size: .78rem; white-space: nowrap; }
.badge.target { background: #2e7d32; color: #fff; }
.badge.flag { background: #c62828; color: #fff; margin-right: .2rem; }
.badge.grade-A { background: #2e7d32; color: #fff; }
.badge.grade-B { background: #558b2f; color: #fff; }
.badge.grade-C { background: #ef6c00; color: #fff; }
.badge.grade-spares { background: #c62828; color: #fff; }
.badge.grade-unknown { background: #9e9e9e; color: #fff; }
ul.manual li { margin: .2rem 0; }
footer { color: #999; font-size: .8rem; margin-top: 2rem; }
"""


def _fmt_price(value) -> str:
    if value is None:
        return "&mdash;"
    return f"&pound;{value:,.2f}".replace(".00", "")


def _grade_badge(grade: str) -> str:
    css = "spares" if grade == "spares/repair" else grade
    return f'<span class="badge grade-{html.escape(css)}">{html.escape(grade)}</span>'


def _row_class(row) -> str:
    flags = json.loads(row["flags"] or "[]")
    if row["grade"] == "spares/repair" or flags:
        return "warning"
    if (row["deal_score"] or 0) >= EXCELLENT_SCORE:
        return "excellent"
    return ""


def _listing_row(row) -> str:
    flags = json.loads(row["flags"] or "[]")
    flag_html = " ".join(
        f'<span class="badge flag">{html.escape(f)}</span>' for f in flags
    ) or "&mdash;"
    target = ' <span class="badge target">under target</span>' if row["under_target"] else ""
    cls = _row_class(row)
    cls_attr = f' class="{cls}"' if cls else ""
    title = html.escape(row["title"])
    url = html.escape(row["url"], quote=True)
    first_seen = html.escape((row["first_seen"] or "")[:10])
    return (
        f"<tr{cls_attr}>"
        f'<td class="score">{row["deal_score"]:.0f}</td>'
        f'<td><a href="{url}">{title}</a></td>'
        f"<td>{_fmt_price(row['price'])}{target}</td>"
        f"<td>{_fmt_price(row['normal_price'])}</td>"
        f"<td>{_fmt_price(row['margin_abs'])}</td>"
        f"<td>{row['margin_pct']:.0f}%</td>"
        f"<td>{_grade_badge(row['grade'])}</td>"
        f"<td>{flag_html}</td>"
        f"<td>{html.escape(row['source'])}</td>"
        f"<td>{first_seen}</td>"
        f"</tr>"
    )


_TABLE_HEAD = (
    "<table><thead><tr>"
    "<th>Score</th><th>Title</th><th>Price</th><th>Normal</th>"
    "<th>Saving</th><th>Saving %</th><th>Grade</th><th>Flags</th>"
    "<th>Source</th><th>First seen</th>"
    "</tr></thead><tbody>"
)


def build_html(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    manual_links: list[ManualLink] | None = None,
) -> str:
    rows = db.report_rows(conn)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<title>Product Finder &mdash; Deal Report</title>",
        f"<style>{_STYLE}</style></head><body>",
        "<h1>Product Finder &mdash; Deal Report</h1>",
        f'<p class="meta">Generated: {now}</p>',
    ]

    if not rows:
        parts.append("<p>No matched listings yet. Run <code>python -m product_finder run-once</code>.</p>")

    current_project = None
    current_item = None
    table_open = False
    for row in rows:
        if row["project_name"] != current_project:
            if table_open:
                parts.append("</tbody></table>")
                table_open = False
            current_project = row["project_name"]
            current_item = None
            parts.append(f"<h2>{html.escape(current_project)}</h2>")
        if row["item_name"] != current_item:
            if table_open:
                parts.append("</tbody></table>")
            current_item = row["item_name"]
            parts.append(f"<h3>{html.escape(current_item)}</h3>")
            parts.append(
                '<p class="meta">'
                f"Normal price: {_fmt_price(row['normal_price'])} &middot; "
                f"Target deal price: {_fmt_price(row['target_deal_price'])} &middot; "
                f"Priority: {html.escape(row['priority'] or 'normal')}</p>"
            )
            parts.append(_TABLE_HEAD)
            table_open = True
        parts.append(_listing_row(row))
    if table_open:
        parts.append("</tbody></table>")

    if manual_links:
        parts.append("<h2>Manual searches</h2>")
        parts.append("<p>These sources are not automated &mdash; open the links to check manually:</p>")
        parts.append('<ul class="manual">')
        for link in manual_links:
            url = html.escape(link.url, quote=True)
            parts.append(f'<li><a href="{url}">{html.escape(link.label)}</a></li>')
        parts.append("</ul>")

    parts.append("<footer>Generated by product-finder.</footer>")
    parts.append("</body></html>")
    return "\n".join(parts)


def html_report_path(cfg: AppConfig) -> Path:
    return Path(cfg.report_path).with_suffix(".html")


def write_html_report(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    manual_links: list[ManualLink] | None = None,
) -> Path:
    path = html_report_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_html(conn, cfg, manual_links))
    return path
