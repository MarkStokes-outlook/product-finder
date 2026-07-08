# Generic spec/category conflict detection — implementation notes

**Date:** 2026-07-08 ~11:54
**Tests:** 484 passing (455 prior + 21 in `test_spec_match.py` + 8 in `test_scoring.py`)
**Trigger:** Mark found a false-positive class he judged to be a weakness in
the matching engine, not the search terms: the item "128GB DDR5 RAM" was
matching laptop listings ("Dell Latitude ... 8GB DDR4 RAM 128GB SSD",
same for ThinkPad/Surface) at a score of ~77 — clearly the wrong product,
sharing surface tokens ("128GB", "RAM") with the search terms but nothing
else. He explicitly didn't want another exclude-term hack; he wanted the
scoring pipeline to understand *why* these are poor matches, generically
(not a RAM special case).

## Where the score was actually coming from

Investigated the whole pipeline before writing anything (`runner.py` →
`scoring.py`). The relevant discovery: **there is no product-identity check
in scoring at all.** `runner.run_once()` takes whatever a source's search
API returns for a term completely on faith; the only per-listing gates
before scoring are `scoring.excluded()` (the item's own exclude-term
substring check) and `catalogue.match()` (which only fires if the item
already has specific catalogue products defined — it didn't, here).
`scoring.deal_score()` itself is purely a *price/condition* score: baseline
+ margin-vs-normal-price + grade adjustment + target bonus + flag/trend
nudges. Nothing about whether the listing is actually the same *kind* of
product. A laptop priced under the item's normal/target price scored
exactly as well as a real RAM stick at the same price — the ~77 traced
directly to `BASELINE_SCORE` (35) + `MARGIN_PLATEAU_SCORE` (30, i.e. a deep
discount) + `TARGET_BONUS` (10) + a grade-B adjustment (+2), with zero
penalty anywhere for "this isn't a memory module."

## What changed

- **`src/product_finder/spec_match.py`** (new): pure extraction and
  contradiction detection, no scoring weights, no RAM-specific code path.
  Three composable mechanisms, all driven by small static keyword tables in
  the same style as `grading.py`'s condition term lists (deterministic
  regex/keyword matching, no ML):
  1. **Capacity-to-component binding.** A "128GB"/"2TB"/"4x32GB" figure is
     bound to whichever component keyword (RAM, SSD, HDD, VRAM, eMMC) sits
     nearest to it in the text (a small forward/backward character window,
     clipped at the neighbouring capacity match so adjacent figures like
     "128GB (2x64GB)" can't cross-contaminate each other's binding). This is
     the direct implementation of "128GB RAM != 128GB SSD" / "16GB VRAM !=
     16GB System RAM" / "2TB SSD != 2TB HDD" — the mechanism is the generic
     part; RAM/SSD/HDD/VRAM/eMMC are just entries in one lookup table,
     trivially extensible to more component types later.
  2. **Technical-attribute families.** Small sets of mutually-exclusive
     values per family (`ram_generation`: ddr2-5, `ram_ecc`: ecc/non_ecc,
     `ram_buffering`: registered/unbuffered, `ram_form_factor`:
     laptop/desktop) — a family only becomes a conflict when *both* sides
     state an explicit, differing value. Silence on one side is never
     evidence of disagreement, only an explicit clash is — directly
     implements "contradictory technical attributes become negative
     evidence" without turning "listing doesn't mention DDR generation"
     into a false penalty.
  3. **Category tagging.** Component keywords double as category tags (a
     text mentioning "RAM" is tagged `ram`); a small system-tier tag set
     (laptop/desktop/tablet/mini_pc/all_in_one) catches complete-system
     listings. Two detectors feed it: keyword-based (laptop/notebook/tablet
     etc., plus a short list of laptop-*only* model families — Latitude,
     ThinkPad, EliteBook, Surface, etc. — deliberately excluding lines like
     XPS/Legion/ROG that span both desktop and laptop and would be a false
     signal) and a fully brand-agnostic one: **a listing whose capacities
     bind to two or more *different* components is, by construction,
     describing a whole system's spec sheet, not a single component for
     sale** — this is what actually catches "Dell Latitude ... 8GB RAM
     128GB SSD" even before any brand-name keyword is considered.
  - `compare(wanted, listing)` returns a flat list of `Conflict(kind,
    message)` — never a positive score. Deliberately: matching capacities
    or agreeing tech attributes earn nothing here, only *contradicting*
    ones cost anything, which is what keeps "numeric token matching should
    not dominate the score" true almost by construction — there's no
    numeric-agreement bonus to dominate with in the first place.
  - One real bug found and fixed while writing tests: the "desktop" system
    keyword originally fired on bare `\bdesktop\b`, which false-positived
    on "Desktop Memory"/"Desktop RAM" — a completely legitimate, common way
    to describe a full-size DIMM (as opposed to a laptop's SO-DIMM), not
    evidence of a complete system. Tightened to require `desktop pc` /
    `desktop computer` / `desktop tower`, or bare `tower`/`workstation`.
    (`ram_form_factor`'s own "desktop" value, a *different* family, still
    reads bare "desktop memory" correctly — that one's about the DIMM
    itself, not the system it's inside.)

- **`src/product_finder/scoring.py`**:
  - `attribute_conflicts(item, listing)` — builds "wanted" text from the
    item's name + search terms + notes (the closest thing an `ItemConfig`
    has to a spec sheet) and runs `spec_match.extract()`/`compare()`
    against the listing's own title/condition/description text.
  - `conflict_penalty(conflicts)` — sums a per-kind severity
    (`category`: 45, `capacity`: 30, `spec`: 25) capped at 75 total. Category
    disagreement is weighted heaviest deliberately — component-vs-whole-
    system is the single most reliable of the three signals — but any one
    conflict already lands a real dent, and several compounding (exactly
    the RAM/laptop case: capacity + generation + category all fire
    together) floors the score.
  - Wired into `evaluate()`: conflicts are computed **only when
    `verified is False`** — the same gate `is_price_implausible()` and the
    margin-decay tail already use. A listing that resolved to a specific,
    human-approved catalogue product is trusted over this generic text
    heuristic; this mechanism is squarely aimed at the class of false
    positive that slips through before any catalogue product exists for an
    item, not a second-guessing of confirmed matches. Conflict messages are
    appended to the existing `flags` list (free UI visibility — these now
    show up as badges and route the listing into the existing "Warnings"
    section / out of "best deals", no template changes needed, since
    `flagged` is already just `flags != []` everywhere in `db.py`) and the
    conflict penalty also disqualifies the target-bonus/`under_target`
    the same way an implausible price or live auction already does.
  - Result end-to-end for the reported case: all three listings
    (Latitude/ThinkPad/Surface) go from ~77 to **0** (baseline 35 + margin
    ~30 + grade ~2, minus a capped 75-point conflict penalty minus the
    generic flags-count penalty, floored at 0), each with three
    human-readable flags (`ram capacity mismatch: wanted 128GB, listing
    says 8GB`, `ram generation mismatch: wanted ddr5, listing says ddr4`,
    `category mismatch: wanted looks like ['ram'], listing looks like
    ['laptop', 'multi_component_system', 'ram', 'storage_ssd']`). A genuine
    matching listing ("Corsair Vengeance 128GB (4x32GB) DDR5 6000MHz
    Desktop Memory") is unaffected — scores 60.7, zero conflict flags.

## Tests

- `tests/test_spec_match.py` (21 tests): capacity/component binding
  (including the adjacent-multiplier edge case and VRAM-vs-RAM/SSD-vs-HDD
  distinctness), tech-attribute families (DDR generation, ECC/non-ECC,
  RDIMM/UDIMM, SO-DIMM/DIMM — including that silence isn't disagreement),
  category tagging (model-family keywords, the brand-agnostic
  multi-component heuristic, and that a single genuine component listing
  is never tagged `multi_component_system`), and `compare()` (all three
  conflict kinds, no false positive on a genuine match, binary/decimal
  capacity rounding tolerance).
- `tests/test_scoring.py` (+8 tests): the three exact reported listings
  (parametrized) now score ≤15 and are never `under_target`; all three
  conflict kinds show up in `evaluate()`'s flags together; a genuine RAM
  listing scores >60 with no conflict flags; an SSD-vs-HDD pairing (a
  different component pair, proving genericity) also scores poorly; a
  verified catalogue-product match is never penalised even when its title
  contains a system-ish word ("desktop tower"); and a direct
  `attribute_conflicts()` call with a GPU/VRAM item (not RAM at all)
  confirms the mechanism isn't RAM-special-cased.

## What's not covered / left as-is

- This is unverified-only by design (see above) — it does not touch
  `catalogue.match()` or anything about how a listing resolves to a
  specific product.
- No new component/system categories beyond what the three reported
  examples and the "things to consider" list actually needed (RAM/VRAM/
  SSD/HDD/eMMC components; laptop/desktop/tablet/mini_pc/all_in_one
  systems). Didn't build out GPU/CPU/motherboard/monitor category
  keyword sets — nothing in the request's examples needed them, and
  guessing at a full hardware taxonomy nobody asked for felt like exactly
  the kind of premature scope the project avoids elsewhere.
- **Live data is not retroactively rescored.** `db.record_match()` already
  recomputes grade/score/flags in place whenever the *same* listing is
  seen again in a future search cycle, so this fix applies automatically
  going forward — but a listing already sitting in `listing_matches` from
  a past scan keeps its old score until it resurfaces in a search. Mark's
  `watch` process (pid 56125 at time of writing) is running the pre-fix
  code in memory and needs a restart to apply this to *future* scans, same
  situation as the `web --debug` restart earlier this session. Not
  restarted by this session. A one-off rescore pass over existing
  `listing_matches` rows would be straightforward to write if wanted, but
  wasn't requested — flagged to Mark rather than run unprompted, since
  (unlike the earlier item-duplicate cleanup) it would touch a
  possibly-large, unknown number of rows on a heuristic just written this
  session.
