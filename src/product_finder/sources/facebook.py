"""Facebook Marketplace source.

No compliant automation route exists (login-walled, no public API), so this
source is manual-assisted: it generates search links to open in a browser.
"""

from __future__ import annotations

from urllib.parse import urlencode

from ..config import ItemConfig
from ..models import ManualLink
from .base import ConnectorKnowledge, Source, SourceCapabilities


class FacebookSource(Source):
    name = "facebook"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            automated=False,
            compliance="manual-assisted links only (login-walled; no "
                       "compliant automation route)",
            account_risk="none",
            compliance_mode="manual",
            can_run_unattended=False,
            # This connector only builds a URL — it never uses a session
            # itself. Whether the human happens to be logged in when they
            # click it is their own browser, not this connector's concern.
            # A future browser/session-based Facebook connector (see Phase 7
            # options paper) would declare requires_user_auth=True and its
            # own non-"none" account_risk — it would not reuse this class.
            requires_user_auth=False,
            requires_manual_input=True,
            recommended_schedule="manual only",
            freshness="unknown",
        )

    def knowledge(self) -> ConnectorKnowledge:
        return ConnectorKnowledge(
            display_name="Facebook Marketplace",
            description="Generates pre-filled Facebook Marketplace search links "
                        "for a human to open. Marketplace is login-walled with "
                        "no compliant automation route, so this connector never "
                        "fetches or parses a listing itself.",
            implementation_type="Static search-link generator (URL templating only, no fetch/parse)",
            maturity="production",
            supported_marketplaces=("Facebook Marketplace",),
            supported_search_features=("Free-text keyword search", "Max price filter"),
            known_limitations=(
                "No location/radius parameter — relies entirely on the "
                "marketplace's own logged-in session location; this "
                "connector never supplies one.",
                "Never sees an actual listing, only builds a URL - "
                "supported_listing_types is empty and every listing-shape "
                "field in the Capabilities checklist reports 'na'.",
            ),
            intentionally_unsupported=(
                "Any session/browser-automation-based access - see "
                "docs/strategy/facebook-gumtree-connector-options.md. Ruled "
                "out as *required* architecture even if a future opt-in "
                "connector is ever built for this; it would not reuse this "
                "class, and it must never be schedulable regardless.",
            ),
            investigation_items=(
                "Third-party data provider (Apify-style) as the most "
                "realistic path to real Facebook coverage without risking "
                "the operator's own account - see options paper.",
            ),
        )

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
