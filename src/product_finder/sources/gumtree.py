"""Gumtree UK source.

Gumtree has no official public API and its terms prohibit scraping, so this
source is manual-assisted: it generates pre-filtered search links to open in
a browser.
"""

from __future__ import annotations

from urllib.parse import urlencode

from ..config import ItemConfig
from ..models import ManualLink
from .base import ConnectorKnowledge, Source, SourceCapabilities


class GumtreeSource(Source):
    name = "gumtree"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            automated=False,
            compliance="manual-assisted links only (terms prohibit scraping; "
                       "no official public API)",
            account_risk="none",
            compliance_mode="manual",
            can_run_unattended=False,
            requires_manual_input=True,
            recommended_schedule="manual only",
            freshness="unknown",
        )

    def knowledge(self) -> ConnectorKnowledge:
        return ConnectorKnowledge(
            display_name="Gumtree",
            description="Generates pre-filled Gumtree search links for a human "
                        "to open and browse. Gumtree's terms prohibit automated "
                        "scraping and it has no official public search API, so "
                        "this connector never fetches or parses a listing itself.",
            implementation_type="Static search-link generator (URL templating only, no fetch/parse)",
            maturity="production",
            supported_marketplaces=("Gumtree UK",),
            supported_search_features=(
                "Free-text keyword search", "Postcode + radius filter",
                "Max price filter",
            ),
            known_limitations=(
                "Never sees an actual listing, only builds a URL - "
                "supported_listing_types is empty and every listing-shape "
                "field in the Capabilities checklist reports 'na', not a "
                "false claim of zero support.",
            ),
            intentionally_unsupported=(
                "Any form of automated fetching/scraping of Gumtree - its "
                "terms prohibit it. See sources/base.py's account_risk model "
                "for how a future risk-acknowledged connector could exist "
                "without silently changing this one's declared risk.",
            ),
            investigation_items=(
                "Official Gumtree API access (none known to exist today).",
            ),
        )

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
