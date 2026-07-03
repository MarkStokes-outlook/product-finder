"""Facebook Marketplace source.

No compliant automation route exists (login-walled, no public API), so this
source is manual-assisted: it generates search links to open in a browser.
"""

from __future__ import annotations

from urllib.parse import urlencode

from ..config import ItemConfig
from ..models import ManualLink
from .base import Source


class FacebookSource(Source):
    name = "facebook"

    def is_automated(self) -> bool:
        return False

    def manual_links(self, item: ItemConfig) -> list[ManualLink]:
        links = []
        for term in item.terms:
            params = {"query": term}
            if item.max_price:
                params["maxPrice"] = f"{item.max_price:g}"
            links.append(
                ManualLink(
                    source=self.name,
                    label=f"Facebook Marketplace: {term}",
                    url=f"https://www.facebook.com/marketplace/search/?{urlencode(params)}",
                )
            )
        return links
