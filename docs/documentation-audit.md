# Documentation Audit

This audit reviews architecture-related documentation after the architecture refresh.

No documents were deleted.

## Canonical Document Set

Current canonical entry points:

- `VISION.md`
- `docs/platform-charter.md`
- `ARCHITECTURE.md`
- `docs/knowledge-model.md`
- `docs/platform-domain-model.md`
- `docs/connector-architecture.md`
- `docs/strategy/roadmap.md`
- `docs/documentation-audit.md`
- `docs/architecture-review.md`
- `docs/strategic-review.md`

Historical/contextual documents:

- `docs/adr/*`
- `docs/implementation-notes/*`
- `docs/design/2026-07-04-fuzzy-duplicate-grouping.md`
- `docs/architecture-briefing.md`
- `docs/strategy/facebook-gumtree-connector-options.md`

## Duplicate Documents

### `ARCHITECTURE.md` and `docs/architecture-briefing.md`

Both describe system architecture.

Recommendation:

Keep `ARCHITECTURE.md` as canonical. Treat `docs/architecture-briefing.md` as an earlier snapshot and field-level/historical briefing. A historical-status banner has been added; do not delete it.

### README and `VISION.md`

README is now the operational front door. `VISION.md` is the canonical product vision.

Recommendation:

README has been shortened into an operational front door and now links to the canonical product and architecture documents.

### ADR roadmap and strategy roadmap

ADR-0001 describes public/commercial phase sequencing. `docs/strategy/roadmap.md` describes platform evolution.

Recommendation:

Keep both. ADR-0001 is sequencing history and rationale. The roadmap is the living platform evolution map.

## Conflicting Documents

### ADR-0007 status vs implementation

ADR-0007 says Catalogue Globalization is proposed/planning-only. Implementation, backlog, tests, and implementation notes show EPIC-100 shipped.

Implementation wins.

Recommendation:

Do not rewrite ADR history silently. ADR-0007 now has a current-state note pointing to the shipped EPIC-100 implementation and canonical current-state docs.

### ADR-0004 current schema statements

ADR-0004 states products were item-scoped and flags the schema gap. That was true when written, but EPIC-100 later fixed it.

Implementation wins.

Recommendation:

Keep the ADR as historical context. ADR-0004 now has a current-state note pointing to `docs/platform-domain-model.md` for current ownership truth.

### Roadmap AI/Ollama statements vs older architecture briefing

`docs/architecture-briefing.md` says no AI/Ollama integration exists. Current implementation has optional Ollama extraction in `extraction.py`, disabled by default, feeding product suggestions only.

Implementation wins.

Recommendation:

Treat architecture briefing's AI section as stale. Canonical docs should describe Ollama as optional suggestion extraction, not scoring or autonomous catalogue mutation.

### Fuzzy duplicate design doc vs current code

The design doc describes same-source duplicate proposal as part of v1. Current `duplicates.py` rejects same-source pairs and only scores cross-marketplace pairs.

Implementation wins.

Recommendation:

Keep the design doc as approved historical design plus implementation-note context. Current behaviour belongs in `ARCHITECTURE.md` and `docs/knowledge-model.md`.

### ADR-0002 original scope vs shipped gateway

ADR-0002 frames outbound work as affiliate redirects for specific templates. Shipped implementation is a general Marketplace Outbound Gateway covering all listing-level template links, including offers and duplicate review.

Implementation wins.

Recommendation:

Keep ADR-0002 as origin decision; use `ARCHITECTURE.md` for current gateway scope.

## Stale Documents

### `docs/architecture-briefing.md`

Stale areas:

- says no Ollama/AI integration
- says no SearXNG integration
- says no generated report/export artefact but does not reflect project import/export v1
- older test counts
- products/domain model partly predates catalogue globalization
- current limitations no longer fully reflect identity/outbound/connector maturity work

Recommendation:

Marked as superseded by `ARCHITECTURE.md` for current state. Keep as historical field-level briefing until all useful details have been migrated.

### ADR-0007

Stale status and planning-only language after EPIC-100 shipped.

Recommendation:

Current-state note added.

### ADR-0004

Stale known schema gap after EPIC-100 shipped.

Recommendation:

Current-state note added.

### README

The README remains useful for setup and usage, but the product description still centres "genuine bargains" and should be aligned with the knowledge-platform framing.

Recommendation:

Completed. README now keeps operational setup detail and links out to canonical platform docs instead of expanding product/architecture prose.

## Superseded Documents

Superseded as canonical current-state architecture:

- `docs/architecture-briefing.md`

Superseded as current-state duplicate behaviour:

- parts of `docs/design/2026-07-04-fuzzy-duplicate-grouping.md`

Superseded as current-state schema:

- current-schema sections of ADR-0004 and ADR-0007

Not superseded:

- implementation notes remain chronological history
- ADRs remain decision records
- Facebook/Gumtree options paper remains analysis-only and useful

## Missing Documents Now Added

- product vision
- platform charter
- knowledge model
- platform domain model
- connector architecture guide
- documentation audit
- architecture review
- strategic review

## Remaining Missing Documents

Recommended later, not implemented by this refresh:

- public/private route threat model before EPIC-104
- global product edit/merge governance note before multi-user catalogue editing
- retention/data lifecycle policy
- source onboarding checklist template
- recommendation-readiness criteria
- operator runbook for backup/restore and migration safety

## Consolidation Recommendation

Do not delete historical docs yet.

Consolidation status:

1. Superseded/current-state banners have been added to `docs/architecture-briefing.md`, ADR-0004, ADR-0007, and the fuzzy duplicate design document.
2. README's vision/product sections have been shortened and link to `VISION.md`, `ARCHITECTURE.md`, `docs/platform-charter.md`, `docs/knowledge-model.md`, and `docs/strategy/roadmap.md`.
3. Keep implementation notes chronological and immutable.
4. Keep ADRs as decisions, but append status notes when implementation has moved past them.
5. Treat `ARCHITECTURE.md` as the only current-state architecture entry point.
