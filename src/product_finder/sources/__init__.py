"""Marketplace sources.

Every source implements the `Source` contract in `base.py`. The registry is
built from config: built-in sources by their enabled flags, plus any number
of config-defined `extra` sources (rss = automated, links = manual-assisted)
— so new endpoints are usually YAML-only, no code.

Scheduling is risk-gated, not just enabled-gated (see build_registry): a
source being "enabled" is not on its own enough to include it in scheduled
background runs once it declares account_risk above "low" — that also
needs an explicit, per-source opt-in. This is deliberate scaffolding for
connectors that don't exist yet (a future scraping/user-session/licensed-
provider connector) — see sources/base.py's SourceCapabilities docstring
and docs/strategy/roadmap.md.
"""

from __future__ import annotations

from ..config import AppConfig
from .base import ACCOUNT_RISK_LEVELS, Source
from .ebay import EbaySource
from .facebook import FacebookSource
from .gumtree import GumtreeSource
from .links import UrlTemplateSource
from .rss import RssSource

_EXTRA_TYPES = {"rss": RssSource, "links": UrlTemplateSource}

#: account_risk levels at or below this are included in scheduled background
#: runs by default. Anything riskier needs an explicit per-source entry in
#: cfg.sources.risk_acknowledged — never included just because it's
#: "enabled". See SourceCapabilities.account_risk.
_DEFAULT_MAX_UNACKNOWLEDGED_RISK = "low"


def _risk_allowed(cfg: AppConfig, name: str, capabilities) -> bool:
    risk = capabilities.account_risk
    if ACCOUNT_RISK_LEVELS.index(risk) <= ACCOUNT_RISK_LEVELS.index(_DEFAULT_MAX_UNACKNOWLEDGED_RISK):
        return True
    # medium/high: must be explicitly named, every time — being enabled is
    # not enough, and there is no "accept everything" switch. This is the
    # same explicit-opt-in mechanism for both medium and high risk; high
    # risk is not treated as a separate, stronger gate because there's
    # nothing weaker to compare it against — the point is that neither is
    # ever silent.
    return name in cfg.sources.risk_acknowledged


def build_registry(cfg: AppConfig) -> dict[str, Source]:
    """Instantiate sources for scheduled background use: enabled, and
    risk-allowed (see _risk_allowed)."""
    candidates: dict[str, Source] = {}
    if cfg.sources.ebay.enabled:
        candidates["ebay"] = EbaySource(cfg)
    if cfg.sources.gumtree_enabled:
        candidates["gumtree"] = GumtreeSource(cfg)
    if cfg.sources.facebook_enabled:
        candidates["facebook"] = FacebookSource(cfg)
    for spec in cfg.sources.extra:
        if spec.enabled:
            candidates[spec.name] = _EXTRA_TYPES[spec.type](cfg, spec)

    registry: dict[str, Source] = {}
    for name, source in candidates.items():
        if _risk_allowed(cfg, name, source.capabilities()):
            registry[name] = source
    return registry


def build_all(cfg: AppConfig) -> dict[str, Source]:
    """Every known connector, enabled or not — for the Sources page's
    capability/compliance/health display. build_registry() stays the
    operational set the runner actually searches."""
    connectors: dict[str, Source] = {
        "ebay": EbaySource(cfg),
        "gumtree": GumtreeSource(cfg),
        "facebook": FacebookSource(cfg),
    }
    for spec in cfg.sources.extra:
        connectors[spec.name] = _EXTRA_TYPES[spec.type](cfg, spec)
    return connectors
