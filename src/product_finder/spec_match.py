"""Generic technical-spec extraction & contradiction detection.

Born from a false-positive class: an item wanting "128GB DDR5 RAM" matching
laptop listings ("Dell Latitude ... 8GB DDR4 RAM 128GB SSD") that share
surface tokens ("128GB", "RAM") with the search terms but are a structurally
different product — a complete system, not a bare memory module, and one
whose own stated RAM capacity/generation actively contradict what's wanted.

Nothing here is specific to RAM. Three independent, composable mechanisms,
each driven by small keyword tables (same style as grading.py's condition
term lists — deterministic, no ML, easy to extend):

1. Capacity-to-component binding: a "128GB" (or "2TB", or "4x32GB") figure is
   bound to whichever component keyword (RAM, SSD, HDD, VRAM, eMMC) sits
   nearest to it in the text, so "128GB RAM" and "128GB SSD" are never
   treated as the same claim just because the number matches.
2. Technical-attribute families: small sets of mutually-exclusive values
   (DDR generation, ECC/non-ECC, registered/unbuffered, SO-DIMM/DIMM) where
   both sides stating a *different* explicit value is contradictory
   evidence. Silence on one side is never treated as disagreement — only an
   explicit clash counts.
3. Category tagging: component keywords double as lightweight category tags
   (a text mentioning "RAM" is tagged "ram"), plus a small set of "this is a
   complete system" signals (laptop/desktop/tablet keywords, well-known
   laptop-only model families, or — fully brand-agnostic — a listing whose
   capacities bind to two or more *different* components at once, which is
   what a system's spec-sheet title looks like and a single component's
   never does).

This module only extracts and compares; it assigns no score. See
scoring.py's `_CONFLICT_SEVERITY` for how a Conflict's `kind` translates into
a score penalty, and docs/implementation-notes/2026-07-08-*-spec-conflict-
detection.md for the reasoning behind the numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CONFLICT_CAPACITY = "capacity"
CONFLICT_SPEC = "spec"
CONFLICT_CATEGORY = "category"

# --- Capacity extraction & component binding --------------------------------

_UNIT_TO_GB = {"tb": 1024.0, "gb": 1.0, "mb": 1.0 / 1024}

# Multiplier form ("4x32GB", "2 x 64GB") checked first since it also matches
# the bare-capacity alternative below (the trailing "32GB" alone) — re
# tries alternatives left-to-right per position, so ordering here matters.
_CAPACITY_RE = re.compile(
    r"\b(?P<mult>\d+)\s*[x×]\s*(?P<mvalue>\d+(?:\.\d+)?)\s*(?P<munit>tb|gb|mb)\b"
    r"|\b(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>tb|gb|mb)\b",
    re.IGNORECASE,
)

# Checked in this order when binding a capacity figure to a component — more
# specific keywords first, so "VRAM" claims a figure before the generic RAM
# family gets a look (its \bram\b wouldn't match inside "vram" anyway, since
# there's no word boundary between "v" and "ram", but specific-first is the
# safer general rule as more component families get added here).
_COMPONENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("vram", re.compile(r"\bvram\b|\bvideo memory\b|\bgraphics memory\b", re.I)),
    ("storage_ssd", re.compile(r"\bssd\b|\bsolid state\b|\bnvme\b|\bm\.2\b", re.I)),
    ("storage_hdd", re.compile(r"\bhdd\b|\bhard drive\b|\bhard disk\b", re.I)),
    ("storage_emmc", re.compile(r"\bemmc\b", re.I)),
    ("ram", re.compile(r"\bram\b|\bmemory\b|\bddr\d\b|\bso-?dimm\b|\budimm\b|\brdimm\b|\bdimm\b", re.I)),
    ("storage", re.compile(r"\bstorage\b", re.I)),
]

# How far (in characters) to look past — or, failing that, before — a
# capacity figure for a component keyword before giving up and treating it
# as unbound (and so never compared against anything). Roughly "a couple of
# words": real listing titles state the component immediately adjacent to
# the figure ("128GB SSD", "RAM: 8GB"), not several clauses away.
_BIND_WINDOW_CHARS = 28


@dataclass(frozen=True)
class Capacity:
    component: str
    value_gb: float
    raw: str


def _match_component(window: str) -> str | None:
    for name, pattern in _COMPONENT_PATTERNS:
        if pattern.search(window):
            return name
    return None


def _capacities(text: str) -> list[Capacity]:
    matches = list(_CAPACITY_RE.finditer(text))
    results = []
    for i, m in enumerate(matches):
        if m.group("mult"):
            value = float(m.group("mult")) * float(m.group("mvalue"))
            unit = m.group("munit").lower()
        else:
            value = float(m.group("value"))
            unit = m.group("unit").lower()
        value_gb = value * _UNIT_TO_GB[unit]

        # Window is clipped at the neighbouring capacity match on either
        # side, so e.g. "128GB (2x64GB) DDR5" can't have the *first* figure
        # wrongly bind across the second one to "DDR5".
        forward_limit = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        component = _match_component(text[m.end():min(m.end() + _BIND_WINDOW_CHARS, forward_limit)])
        if component is None:
            backward_limit = matches[i - 1].end() if i > 0 else 0
            component = _match_component(text[max(m.start() - _BIND_WINDOW_CHARS, backward_limit):m.start()])
        if component is not None:
            results.append(Capacity(component=component, value_gb=value_gb, raw=m.group(0)))
    return results


# --- Technical-attribute families -------------------------------------------

# Each family: a name, and (value, pattern) pairs checked in order — the
# *first* matching value wins, so more specific patterns (e.g. "SO-DIMM",
# which also mentions "DIMM") must come before more general ones in the same
# family. Two sides disagreeing on a family only counts as a conflict if
# *both* stated an explicit value — see compare().
_TECH_FAMILIES: list[tuple[str, list[tuple[str, re.Pattern]]]] = [
    ("ram_generation", [
        ("ddr5", re.compile(r"\bddr5\b", re.I)),
        ("ddr4", re.compile(r"\bddr4\b", re.I)),
        ("ddr3", re.compile(r"\bddr3\b", re.I)),
        ("ddr2", re.compile(r"\bddr2\b", re.I)),
    ]),
    ("ram_ecc", [
        ("non_ecc", re.compile(r"\bnon[\s-]?ecc\b", re.I)),
        ("ecc", re.compile(r"\becc\b", re.I)),
    ]),
    ("ram_buffering", [
        ("registered", re.compile(r"\brdimm\b|\bregistered\b", re.I)),
        ("unbuffered", re.compile(r"\budimm\b|\bunbuffered\b", re.I)),
    ]),
    ("ram_form_factor", [
        # SO-DIMM checked before bare DIMM in the *same* family, so a
        # "SO-DIMM" title resolves this family to "laptop" and never even
        # tests the "desktop" pattern against it.
        ("laptop", re.compile(r"\bso-?dimm\b|\blaptop memory\b|\bnotebook memory\b", re.I)),
        ("desktop", re.compile(r"\bdesktop memory\b|\budimm\b|\bdimm\b", re.I)),
    ]),
]


def _tech_tags(text: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for family, values in _TECH_FAMILIES:
        for value, pattern in values:
            if pattern.search(text):
                tags[family] = value
                break
    return tags


# --- Category tagging --------------------------------------------------------

# Component keywords (below) double as category tags; this is just the
# "this is a complete system" side. A modest, deliberately laptop-leaning
# list — the model families here are laptop-only lines (unlike e.g. "XPS" or
# "Legion", which span both desktop and laptop and would be a false signal).
_SYSTEM_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("laptop", re.compile(
        r"\blaptop\b|\bnotebook\b|\bultrabook\b|\bchromebook\b|\bmacbook\b|"
        r"\blatitude\b|\bthinkpad\b|\bthinkbook\b|\belitebook\b|\bprobook\b|\bzbook\b|"
        r"\bideapad\b|\bvivobook\b|\bzenbook\b|\bspectre\b|\bsurface\b",
        re.I,
    )),
    # Bare "desktop" is deliberately excluded — "desktop memory"/"desktop
    # RAM" is a common, legitimate way to describe a full-size DIMM (as
    # opposed to a laptop's SO-DIMM), not evidence of a complete system.
    # Only count it paired with a noun that actually means "a computer".
    ("desktop", re.compile(r"\bdesktop\s*(pc|computer|tower)\b|\btower\b|\bworkstation\b", re.I)),
    ("tablet", re.compile(r"\btablet\b|\bipad\b", re.I)),
    ("mini_pc", re.compile(r"\bmini[\s-]?pc\b|\bnuc\b", re.I)),
    ("all_in_one", re.compile(r"\ball[\s-]?in[\s-]?one\b|\baio\b", re.I)),
]
SYSTEM_CATEGORIES = frozenset(name for name, _ in _SYSTEM_PATTERNS) | {"multi_component_system"}
COMPONENT_CATEGORIES = frozenset({"ram", "vram", "storage_ssd", "storage_hdd", "storage_emmc", "storage"})


def _categories(text: str, capacities: list[Capacity]) -> set[str]:
    cats = {name for name, pattern in _SYSTEM_PATTERNS if pattern.search(text)}
    cats |= {c.component for c in capacities}
    # Fully brand-agnostic system signal: capacities bound to two or more
    # *different* components in one title is what a system's spec sheet
    # looks like ("8GB RAM, 128GB SSD") — a single component listing only
    # ever states its own capacity.
    if len({c.component for c in capacities}) >= 2:
        cats.add("multi_component_system")
    return cats


# --- Public API ----------------------------------------------------------------


@dataclass
class ExtractedAttributes:
    capacities: list[Capacity] = field(default_factory=list)
    tech_tags: dict[str, str] = field(default_factory=dict)
    categories: set[str] = field(default_factory=set)


def extract(text: str) -> ExtractedAttributes:
    text = (text or "").lower()
    capacities = _capacities(text)
    return ExtractedAttributes(
        capacities=capacities,
        tech_tags=_tech_tags(text),
        categories=_categories(text, capacities),
    )


@dataclass(frozen=True)
class Conflict:
    kind: str  # CONFLICT_CAPACITY | CONFLICT_SPEC | CONFLICT_CATEGORY
    message: str


# Capacities within this fraction of each other are treated as the same
# figure — covers binary-vs-decimal GB/TB rounding ("1TB" vs "1000GB"), not
# genuinely different capacities (128GB vs 8GB is nowhere near this close).
_CAPACITY_TOLERANCE_PCT = 0.03


def _capacities_conflict(a: float, b: float) -> bool:
    return a != b and abs(a - b) > _CAPACITY_TOLERANCE_PCT * max(a, b)


def _by_component(capacities: list[Capacity]) -> dict[str, float]:
    result: dict[str, float] = {}
    for c in capacities:
        result.setdefault(c.component, c.value_gb)
    return result


def _fmt_gb(value: float) -> str:
    if value >= 1024 and value % 1024 == 0:
        return f"{int(value / 1024)}TB"
    return f"{value:g}GB"


def compare(wanted: ExtractedAttributes, listing: ExtractedAttributes) -> list[Conflict]:
    """Contradictions found between what an item wants and what a listing
    states — never a positive match bonus, only negative evidence. (A
    listing simply *not* mentioning something the item wants is not, by
    itself, a conflict — only an explicit, differing claim counts.)"""
    conflicts: list[Conflict] = []

    wanted_caps = _by_component(wanted.capacities)
    listing_caps = _by_component(listing.capacities)
    for component, w_value in wanted_caps.items():
        l_value = listing_caps.get(component)
        if l_value is not None and _capacities_conflict(w_value, l_value):
            conflicts.append(Conflict(
                CONFLICT_CAPACITY,
                f"{component.replace('_', ' ')} capacity mismatch: wanted "
                f"{_fmt_gb(w_value)}, listing says {_fmt_gb(l_value)}",
            ))

    for family, w_value in wanted.tech_tags.items():
        l_value = listing.tech_tags.get(family)
        if l_value is not None and l_value != w_value:
            conflicts.append(Conflict(
                CONFLICT_SPEC,
                f"{family.replace('_', ' ')} mismatch: wanted {w_value}, listing says {l_value}",
            ))

    if wanted.categories and listing.categories:
        component_wanted = wanted.categories & COMPONENT_CATEGORIES
        system_listed = listing.categories & SYSTEM_CATEGORIES
        if wanted.categories.isdisjoint(listing.categories) or (component_wanted and system_listed):
            conflicts.append(Conflict(
                CONFLICT_CATEGORY,
                f"category mismatch: wanted looks like {sorted(wanted.categories)}, "
                f"listing looks like {sorted(listing.categories)}",
            ))

    return conflicts
