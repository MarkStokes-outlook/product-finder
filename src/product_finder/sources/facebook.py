"""Facebook Marketplace source.

No compliant automation route exists (login-walled, no public API), so this
source is manual-assisted: it generates search links to open in a browser.
"""

from __future__ import annotations

from urllib.parse import urlencode

from ..config import AppConfig, ItemConfig
from ..models import Listing, ManualLink

NAME = "facebook"


def is_automated(cfg: AppConfig) -> bool:
    return False


def search(term: str, item: ItemConfig, cfg: AppConfig) -> list[Listing]:
    return []


def manual_links(item: ItemConfig, cfg: AppConfig) -> list[ManualLink]:
    links = []
    for term in item.terms:
        params = {"query": term}
        if item.max_price:
            params["maxPrice"] = f"{item.max_price:g}"
        links.append(
            ManualLink(
                source=NAME,
                label=f"Facebook Marketplace: {term}",
                url=f"https://www.facebook.com/marketplace/search/?{urlencode(params)}",
            )
        )
    return links
