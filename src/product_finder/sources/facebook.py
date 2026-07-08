"""Facebook Marketplace source.

No compliant automation route exists (login-walled, no public API), so this
source is manual-assisted: it generates search links to open in a browser.
"""

from __future__ import annotations

from urllib.parse import urlencode

from ..config import ItemConfig
from ..models import ManualLink
from .base import Source, SourceCapabilities


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
