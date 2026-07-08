"""Generic URL-template source — manual-assisted, defined entirely in config.

For any site with a public search URL, e.g.:
  - John Pye:  https://www.johnpye.co.uk/?s={term}
  - Vinted:    https://www.vinted.co.uk/catalog?search_text={term}&price_to={max_price}

Templates may use {term}, {max_price}, {postcode}, {radius}.
"""

from __future__ import annotations

from ..config import ExtraSourceConfig, ItemConfig
from ..models import ManualLink
from .base import ConnectorKnowledge, Source, SourceCapabilities
from .rss import format_url


class UrlTemplateSource(Source):
    def __init__(self, cfg, spec: ExtraSourceConfig):
        super().__init__(cfg)
        self.name = spec.name
        self.spec = spec

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            automated=False,
            compliance="manual-assisted search links only",
            account_risk="none",
            compliance_mode="manual",
            can_run_unattended=False,
            requires_manual_input=True,
            recommended_schedule="manual only",
        )

    def knowledge(self) -> ConnectorKnowledge:
        label = self.spec.label or self.spec.name.replace("-", " ").title()
        return ConnectorKnowledge(
            display_name=label,
            description=f"Generic URL-template connector, configured for "
                        f"{label} ({self.spec.url}). Builds a pre-filled "
                        f"search link for a human to open - never fetches "
                        f"or parses anything itself.",
            implementation_type="Static search-link generator (URL "
                                "templating only, no fetch/parse, "
                                "config-driven)",
            # Simpler and lower-risk than the RSS parser (no network call,
            # no parsing at all - purely string substitution), so "production"
            # is honest for the mechanism regardless of which site it points at.
            maturity="production",
            supported_marketplaces=(label,),
            supported_search_features=(
                "URL templating ({term}/{max_price}/{postcode}/{radius} "
                "substituted into the configured URL - see rss.format_url()); "
                "actual search behaviour is whatever the target site's own "
                "search page supports once opened.",
            ),
            known_limitations=(
                "Never sees an actual listing, only builds a URL - "
                "supported_listing_types is empty and every listing-shape "
                "field in the Capabilities checklist reports 'na'.",
            ),
        )

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
