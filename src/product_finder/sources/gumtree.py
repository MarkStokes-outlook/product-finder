"""Gumtree UK source.

Gumtree has no official public API and its terms prohibit scraping, so this
source is manual-assisted: it generates pre-filtered search links to open in
a browser.
"""

from __future__ import annotations

from urllib.parse import urlencode

from ..config import ItemConfig
from ..models import ManualLink
from .base import Source


class GumtreeSource(Source):
    name = "gumtree"

    def is_automated(self) -> bool:
        return False

    def manual_links(self, item: ItemConfig) -> list[ManualLink]:
        links = []
        for term in item.terms:
            params = {"search_category": "all", "q": term}
            if self.cfg.postcode:
                params["search_location"] = self.cfg.postcode
                params["distance"] = str(self.cfg.radius_miles)
            if item.max_price:
                params["max_price"] = f"{item.max_price:g}"
            links.append(
                ManualLink(
                    source=self.name,
                    label=f"Gumtree: {term}",
                    url=f"https://www.gumtree.com/search?{urlencode(params)}",
                )
            )
        return links
