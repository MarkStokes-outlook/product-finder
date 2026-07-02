"""Marketplace sources.

Each source module exposes:
- NAME: str
- is_automated(cfg) -> bool
- search(term, item, cfg) -> list[Listing]   (automated sources only)
- manual_links(item, cfg) -> list[ManualLink]
"""

from . import ebay, facebook, gumtree

ALL = {ebay.NAME: ebay, gumtree.NAME: gumtree, facebook.NAME: facebook}
