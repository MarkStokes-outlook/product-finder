"""Generic URL-template source — manual-assisted, defined entirely in config.

For any site with a public search URL, e.g.:
  - John Pye:  https://www.johnpye.co.uk/?s={term}
  - Vinted:    https://www.vinted.co.uk/catalog?search_text={term}&price_to={max_price}

Templates may use {term}, {max_price}, {postcode}, {radius}.
"""

from __future__ import annotations

from ..config import ExtraSourceConfig, ItemConfig
from ..models import ManualLink
from .base import Source
from .rss import format_url


class UrlTemplateSource(Source):
    def __init__(self, cfg, spec: ExtraSourceConfig):
        super().__init__(cfg)
        self.name = spec.name
        self.spec = spec

    def is_automated(self) -> bool:
        return False

    def manual_links(self, item: ItemConfig) -> list[ManualLink]:
        label = self.spec.label or self.spec.name.replace("-", " ").title()
        return [
            ManualLink(
                source=self.name,
                label=f"{label}: {term}",
                url=format_url(self.spec.url, term, item, self.cfg),
            )
            for term in item.terms
        ]
