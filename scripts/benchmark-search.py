"""Capture and score a bounded eBay search comparison without running the watcher."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from product_finder import db
from product_finder.config import ItemConfig, load_config
from product_finder.sources.ebay import EbaySource


# Start with existing watches. The third query and sort order provide a
# separately sampled reference set for human labelling.
WATCHES = [
    ("Track Saw", 450, "track saw", "Makita SP6000J", "plunge cut circular saw"),
    ("Sliding Mitre Saw", 350, "sliding mitre saw", "DeWalt DWS780", "sliding compound mitre saw"),
    ("NVIDIA RTX 4090", 1400, "4090", "geforce rtx 4090", "rtx 4090 24gb"),
    ("NVIDIA RTX 4080", 850, "4080", "geforce rtx 4080", "rtx 4080 16gb"),
    ("128GB DDR5 RAM", 350, "128gb ddr5 ram", "ddr5 128gb 4x32gb", "128gb ddr5 memory kit"),
    ("4TB NVMe SSD", 275, "4tb ssd", "4tb nvme m.2", "4tb pcie solid state drive"),
    ("1000W PSU", 180, "1000w psu", "corsair rm1000x", "1000 watt atx power supply"),
    ("Samsung Odyssey Neo G9 57", 1300, "odyssey neo g9", "samsung G95NC 57", "LS57CG952NUXXU"),
]


def capture(config: Path, database: Path | None, output: Path) -> None:
    cfg = load_config(config)
    if database:
        uri = database.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            cfg = db.effective_config(connection, cfg)
    connector = EbaySource(cfg)
    if not connector.is_automated():
        raise SystemExit("Configure an automated eBay connector before taking a capture.")

    watches = {}
    started = datetime.now(timezone.utc)
    for name, maximum, broad, focused, reference in WATCHES:
        item = ItemConfig(name, [broad, focused], max_price=maximum)
        queries = {}
        for strategy, term in (("broad", broad), ("focused", focused), ("reference", reference)):
            start = time.monotonic()
            is_reference = strategy == "reference"
            page = connector.search_page(term, item, limit=10 if is_reference else 50,
                                         sort="newlyListed" if is_reference else None)
            queries[strategy] = {
                "query": term,
                "returned": page.returned,
                "total": page.total,
                "has_more": page.has_more,
                "duration_ms": round((time.monotonic() - start) * 1000),
                "candidates": [
                    {"id": listing.external_id, "title": listing.title, "price": listing.price,
                     "options": listing.buying_options, "group": listing.variation_group,
                     "category": listing.category, "url": listing.url}
                    for listing in page.listings if listing.external_id
                ],
            }
        watches[name] = {"max_price": maximum, "queries": queries}
        print(f"Captured {name}", flush=True)
    output.write_text(json.dumps({
        "captured_at": started.isoformat(timespec="seconds"),
        "environment": cfg.sources.ebay.env,
        "watches": watches,
    }, indent=2) + "\n")


def score(capture_path: Path, labels_path: Path) -> None:
    watches = json.loads(capture_path.read_text())["watches"]
    labels = json.loads(labels_path.read_text())
    print("Watch\tReference positives\tBroad hits\tFocused hits\tCombined hits")
    counts = [0, 0, 0, 0]
    for name, watch in watches.items():
        if name not in labels:
            raise SystemExit(f"Reference labels missing for {name}")
        query_ids = {
            name: {row["id"] for row in query["candidates"]}
            for name, query in watch["queries"].items()
        }
        eligible = set(labels[name])
        if not eligible <= query_ids["reference"]:
            raise SystemExit(f"{name} contains labels outside the reference sample")
        values = [
            len(eligible),
            len(eligible & query_ids["broad"]),
            len(eligible & query_ids["focused"]),
            len(eligible & (query_ids["broad"] | query_ids["focused"])),
        ]
        counts = [subtotal + amount for subtotal, amount in zip(counts, values)]
        print(name, *values, sep="\t")
    print("Total", *counts, sep="\t")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("capture")
    live.add_argument("--config", type=Path, required=True)
    live.add_argument("--database", type=Path, help="Existing SQLite database for source overrides, opened read-only")
    live.add_argument("--output", type=Path, required=True)
    offline = commands.add_parser("score")
    offline.add_argument("--capture", type=Path, required=True)
    offline.add_argument("--labels", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        capture(args.config, args.database, args.output)
    else:
        score(args.capture, args.labels)
