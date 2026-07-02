"""Gumtree UK source.

Gumtree has no official public API and its terms prohibit scraping, so this
source is manual-assisted: it generates pre-filtered search links to open in
a browser.
"""

from __future__ import annotations

from urllib.parse import urlencode

from ..config import AppConfig, ItemConfig
from ..models import Listing, ManualLink

NAME = "gumtree"


def is_automated(cfg: AppConfig) -> bool:
    return False


def search(term: str, item: ItemConfig, cfg: AppConfig) -> list[Listing]:
    return []


def manual_links(item: ItemConfig, cfg: AppConfig) -> list[ManualLink]:
    links = []
    for term in item.terms:
        params = {"search_category": "all", "q": term}
        if cfg.postcode:
            params["search_location"] = cfg.postcode
            params["distance"] = str(cfg.radius_miles)
        if item.max_price:
            params["max_price"] = f"{item.max_price:g}"
        links.append(
            ManualLink(
                source=NAME,
                label=f"Gumtree: {term}",
                url=f"https://www.gumtree.com/search?{urlencode(params)}",
            )
        )
    return links
