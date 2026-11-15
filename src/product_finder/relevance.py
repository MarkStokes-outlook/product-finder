"""Determine whether a listing describes the wanted product before scoring it."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import catalogue, spec_match
from .config import ItemConfig
from .models import Listing


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str

    @property
    def eligible(self) -> bool:
        return self.status in ("exact", "substitute")


_RELATED = re.compile(
    r"\b(?:with|for|fits|compatible with|compatible for|compatible)\b|\s\+\s",
    re.I,
)
_GPU = re.compile(r"\b(?:rtx|gtx|rx)[ -]?a?\d{4}(?:d|[ -]?(?:ti(?:[ -]?super)?|super))?\b", re.I)
_POWER = re.compile(r"\b(\d{3,4})\s*(?:w|watts?)\b", re.I)
_STORAGE_SIZE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(tb|gb)\b", re.I)
_PSU = re.compile(r"\bpsu\b|\bpower supply\b", re.I)
_STORAGE = re.compile(r"\b(?:ssd|nvme|solid state drive)\b", re.I)
_NVME = re.compile(r"\bnvme\b", re.I)
_SATA = re.compile(r"\bsata\b", re.I)
_SAW = re.compile(r"\bmit(?:re|er) saw\b", re.I)
_SLIDING_SAW = re.compile(r"\bslid(?:e|ing)\b.{0,36}\bmit(?:re|er) saw\b", re.I)
_SYSTEM = re.compile(
    r"\b(?:(?:gaming|desktop|complete) pc|workstation|poweredge|server|laptop|notebook)\b", re.I,
)
_PC_ACCESSORY = re.compile(
    r"\b(?:cables?|water[ -]?blocks?|adapters?|risers?|backplates?|heatsinks?|shrouds?)\b", re.I,
)
_UNRELATED = re.compile(r"\b(?:gearbox|clutch kit|thread roller|distro plate)\b", re.I)
_SAW_ACCESSORY = re.compile(r"\b(?:stands?|blades?|mounting brackets?)\b", re.I)
_INCH_SIZE = re.compile(r'(?<=\d)\s*(?:-?inch\b|in\b|")', re.I)


def _contains(title: str, phrase: str) -> bool:
    pattern = catalogue.term_pattern(_INCH_SIZE.sub("", phrase))
    return pattern is not None and pattern.search(_INCH_SIZE.sub("", title.lower())) is not None


def _gpu_models(title: str) -> set[str]:
    return {catalogue.model_key(model.group()) for model in _GPU.finditer(title)}


def _sizes(title: str) -> set[float]:
    return {float(value) * (1024 if unit.lower() == "tb" else 1)
            for value, unit in _STORAGE_SIZE.findall(title)}


def assess(listing: Listing, item: ItemConfig, products: list[catalogue.Product]) -> tuple[Decision, catalogue.Product | None]:
    """Use evidence about the advertised product, before compatibility and extras."""
    title = _RELATED.split(listing.title, maxsplit=1)[0].strip()
    wanted = item.name
    gpu = _gpu_models(wanted)
    psu = bool(_PSU.search(wanted) or re.search(r"\bpsu\b", " ".join(item.required_terms), re.I))
    storage = bool(_STORAGE.search(wanted))
    wanted_specs = spec_match.extract(wanted)
    ram = "ram" in wanted_specs.categories or bool(re.search(r"\b(?:ram|ddr[2-5])\b", wanted, re.I))
    category = listing.category

    if gpu or psu or storage or ram:
        if _SYSTEM.search(title) and not (
            ram and re.search(r"\b(?:server|laptop|notebook) (?:ram|memory)\b", title, re.I)
        ):
            return Decision("excluded", "The listing describes a complete computer."), None
        if _UNRELATED.search(title):
            return Decision("excluded", "The listing is for a different product category."), None
        if _PC_ACCESSORY.search(title):
            return Decision("excluded", "The listing describes a part or accessory."), None
        if re.search(r"\b(?:components? & parts|cables? & connectors)\b", category, re.I) and re.search(
            r"\b(?:parts only|replacement|repair kit)\b", title, re.I,
        ):
            return Decision("excluded", "The marketplace category and title identify a replacement part."), None
    if _SAW.search(wanted) and _SAW_ACCESSORY.search(title):
        return Decision("excluded", "The listing describes a saw accessory."), None
    if listing.variation_group or (storage and len(_sizes(listing.title)) > 1):
        return Decision("review", "The listing advertises multiple variants."), None

    product = catalogue.match(title, products)
    alternatives = [phrase for phrase in item.alternatives if _contains(title, phrase)]
    required_missing = [phrase for phrase in item.required_terms if not _contains(title, phrase)]
    if required_missing:
        return Decision("review", "Required evidence missing: " + ", ".join(required_missing)), None

    evidence = bool(alternatives) or _contains(title, wanted) or any(
        _contains(title, term) for term in item.terms
    )
    if product and product.model and _contains(title, product.model):
        evidence = True

    if gpu:
        models = _gpu_models(title)
        approved = set().union(*(_gpu_models(phrase) for phrase in alternatives))
        if models and not models & (gpu | approved):
            return Decision("excluded", "The advertised GPU model differs from the watch."), None
        if len(models) > 1:
            return Decision("review", "The listing names more than one GPU model."), None
        evidence = bool(models & (gpu | approved))
        if models & gpu:
            alternatives = []
        expected_memory = _sizes(wanted)
        if expected_memory and _sizes(title) and not expected_memory & _sizes(title):
            return Decision("excluded", "The advertised GPU memory differs."), None
        evidence = evidence and expected_memory <= _sizes(title)
    if psu and _POWER.search(wanted):
        watts = _POWER.search(wanted).group(1)
        advertised = set(_POWER.findall(title))
        if advertised and watts not in advertised:
            return Decision("excluded", "The advertised power supply wattage differs."), None
        evidence = bool(watts in advertised and _PSU.search(title))
    if storage and _NVME.search(wanted):
        if _SATA.search(title):
            return Decision("excluded", "The drive uses SATA rather than NVMe."), None
        if _sizes(wanted) and _sizes(title) and not _sizes(wanted) & _sizes(title):
            return Decision("excluded", "The advertised drive capacity differs."), None
        evidence = bool(_NVME.search(title) and _sizes(wanted) <= _sizes(title))
    if ram:
        found = spec_match.extract(title)
        conflicts = spec_match.compare(wanted_specs, found)
        if conflicts:
            return Decision("excluded", conflicts[0].message), None
        wanted_sizes = {x.value_gb for x in wanted_specs.capacities if x.component == "ram"}
        found_sizes = {x.value_gb for x in found.capacities if x.component == "ram"}
        evidence = (
            "ram" in found.categories and wanted_sizes <= found_sizes
            and wanted_specs.tech_tags.items() <= found.tech_tags.items()
        )
    if _SLIDING_SAW.search(wanted):
        evidence = bool(_SLIDING_SAW.search(title))
    monitor_size = re.search(r'\b(\d{2})\s*(?:-?inch\b|in\b|")|\bg9\s+(49|57)\b', wanted, re.I)
    if monitor_size:
        size = monitor_size.group(1) or monitor_size.group(2)
        evidence = evidence and bool(re.search(
            rf'(?<!\d){size}(?:\s*(?:-?inch\b|in\b|")|\b)', title, re.I,
        ))

    if alternatives and evidence:
        return Decision("substitute", f"Accepted alternative: {alternatives[0]}."), product
    if evidence:
        return Decision("exact", "The advertised product matches the watch."), product
    return Decision("review", "The advertised product or required specification is unclear."), None
