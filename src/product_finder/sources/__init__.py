"""Marketplace sources.

Every source implements the `Source` contract in `base.py`. The registry is
built from config: built-in sources by their enabled flags, plus any number
of config-defined `extra` sources (rss = automated, links = manual-assisted)
— so new endpoints are usually YAML-only, no code.
"""

from __future__ import annotations

from ..config import AppConfig
from .base import Source
from .ebay import EbaySource
from .facebook import FacebookSource
from .gumtree import GumtreeSource
from .links import UrlTemplateSource
from .rss import RssSource

_EXTRA_TYPES = {"rss": RssSource, "links": UrlTemplateSource}


def build_registry(cfg: AppConfig) -> dict[str, Source]:
    """Instantiate all enabled sources, keyed by name."""
    registry: dict[str, Source] = {}
    if cfg.sources.ebay.enabled:
        registry["ebay"] = EbaySource(cfg)
    if cfg.sources.gumtree_enabled:
        registry["gumtree"] = GumtreeSource(cfg)
    if cfg.sources.facebook_enabled:
        registry["facebook"] = FacebookSource(cfg)
    for spec in cfg.sources.extra:
        if spec.enabled:
            registry[spec.name] = _EXTRA_TYPES[spec.type](cfg, spec)
    return registry
