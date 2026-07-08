# Facebook Marketplace / Gumtree — connector options paper

**Status: analysis only. Nothing in this document is built.** Manual-assisted
link generation (option C) is the only one of these six options actually
shipped for Facebook or Gumtree today — see `sources/facebook.py`,
`sources/gumtree.py`.

## Why this exists

The compliance stance in this project changed from an absolute "don't build
what requires scraping" rule to an explicit risk model (see
`sources/base.py`'s `SourceCapabilities` — `account_risk`, `compliance_mode`,
`is_scraping_based`, etc., and the scheduler-side risk gate in
`sources/__init__.py`). That model can represent a scraping or user-session
connector safely, but this project should not design as if such a connector
can never exist. This paper is the options survey Mark asked for before any
of that gets built — six options, each scored on the same axes, so a future
decision to build one is deliberate rather than defaulted into.

**Verification note:** the eBay work earlier in this phase was verified
against the real, live API using this project's own credentials. Facebook's
and Gumtree's official-API and third-party-provider options below were *not*
live-tested in this session — no Meta Content Library / Apify account exists
in this project to test against. Those sections are reasoned from each
platform's publicly documented access model, not a live capture, and are
flagged as such throughout. Anyone revisiting this should verify current
terms before acting on it — platform API access changes over time.

## Comparison table

| | A. Official/licensed API | B. Indexed search (SearXNG) | C. Manual-assisted | D. Third-party provider | E. User-session/browser automation | F. Direct scraping |
|---|---|---|---|---|---|---|
| **Status** | Not available | Not built | **Shipped** | Not built | Not built | Not built |
| **Data it can get** | N/A | Facebook: near-none (Marketplace isn't publicly indexed, login-walled). Gumtree: title/URL/snippet, sometimes price, from whatever Google/Bing has crawled | None automatically — a human reads the real listing page | Rich (title, price, images, description) — whatever the third-party actor extracts | Richest — full listing detail, same as a logged-in human sees | Facebook: little to none without a session (login wall). Gumtree: full public listing HTML |
| **Can run unattended** | N/A | Yes | No | Yes | Technically yes — **must not be scheduled regardless, per explicit instruction** | Yes for Gumtree; effectively no for Facebook (see below) |
| **Auth requirements** | Would need an approved developer/research account | None (uses this project's own SearXNG instance) | None (human's own browser/session, outside this connector's concern) | An API token for the provider (e.g. Apify) — not a personal marketplace login | A real personal Facebook account's logged-in session | None for Gumtree; effectively requires E for Facebook |
| **Account risk** | N/A | none/low | none | **medium** — no personal account at risk, but the pipeline depends on a service whose business model is scraping against the platform's terms | **high** — real risk of the personal account being restricted/banned | **medium** for Gumtree (project/IP-level ToS exposure, no personal account at stake); collapses to E's risk for Facebook |
| **Freshness** | N/A | Hours to days (search-index lag) | Whenever a human checks | Minutes to hours | Real-time, if run | Real-time, if run |
| **Operational fragility** | N/A | Low-medium (depends on SearXNG's underlying engines staying healthy — see `retailer_price.py` notes on engine reliability) | None (no moving parts) | Medium-high — third-party actor breaks whenever the target site's markup/anti-bot changes; you're dependent on someone else's maintenance | High — sessions expire, 2FA/challenge prompts, active bot-fingerprinting; needs constant babysitting | Medium-high — breaks on markup changes, both platforms can escalate to IP blocking |
| **Implementation complexity** | N/A | Low — same pattern as `retailer_price.py`, reusable | None (already built) | Low-medium — mostly HTTP polling against the provider's API, similar shape to any automated connector; adds a paid external dependency | High — browser automation, session/cookie management, anti-detection | Medium — no browser needed for Gumtree (plain HTTP+HTML parsing), but ongoing markup maintenance and careful rate limiting |
| **Recommended schedule/rate limits** | N/A | Same conservative cadence as `retailer_price.py` (e.g. 24h refresh) — not a real-time feed | On-demand only | Hourly at most, aligned to the provider's own actor run schedule; respect the provider's rate limits | If ever built: manual "check now" only, never a scheduled cadence | Conservative (a few requests/minute), exponential backoff on any sign of blocking — reuse `rate_limit.py` |
| **Enabled by default** | No — not available | No — coverage too sparse/stale to justify by default | **Yes — already the default** | No — explicit opt-in (medium risk + real per-use cost) | No — must never be silently enabled, and must never be schedulable regardless of opt-in | No — explicit opt-in (medium risk) |

## Per-option detail

### A. Official/licensed API — not available for either platform

Facebook's Marketplace was never opened as a public third-party API for
buyer-side search — the Graph API's commerce/catalog endpoints are for
sellers advertising their own inventory, not for searching Marketplace
listings. Meta's "Content Library and API" (via the Meta Researcher
Platform) grants qualified academic/research institutions access to public
content (posts, ads) — it does not cover Marketplace, and requires an
institutional application process, not something this project could obtain
as an individual operator. Gumtree has no documented public search API for
third parties today. **Not revisited unless either platform's official
access model changes.**

### B. Indexed search via SearXNG

This project already has a working pattern for this — `retailer_price.py`
uses Mark's self-hosted SearXNG instance to find retailer prices via
structured-data extraction from search results. The same mechanism could
attempt `site:facebook.com/marketplace <term>` or `site:gumtree.com <term>`
searches. Facebook Marketplace is realistically a dead end here: Marketplace
listings sit behind a login wall and are not meaningfully indexed by the
search engines SearXNG queries. Gumtree listing pages are ordinary public
HTML and stand a real (if partial and stale) chance of being indexed —
worth a small, cheap experiment if Gumtree coverage becomes a priority, using
the exact two-stage human-review pattern `retailer_price.py` already
established (search → candidate → human approves → refresh only the
approved one), not a blind auto-ingest.

### C. Manual-assisted — shipped

`sources/facebook.py` / `sources/gumtree.py` generate pre-filled search
links for a human to open and browse themselves. Zero automation, zero
account risk, zero data captured automatically. This stays the default for
both platforms.

### D. Third-party provider (e.g. Apify)

Apify (and similar services) run marketplace-scraping "actors" on their own
infrastructure and expose the results via an API — this project would poll
Apify's API, not touch Facebook/Gumtree directly. This shifts *technical*
risk away from Mark's own accounts/IPs, but does not make the underlying
activity compliant: the data pipeline is still downstream of a third party
scraping Facebook against its terms, which is why this is modelled as
`is_third_party_provider=True` with `account_risk="medium"`, not "none" —
paying someone else to take the ToS risk is not the same as there being no
risk. It also introduces a new paid, external dependency whose reliability
this project doesn't control. This is the most realistic path to real
Facebook Marketplace coverage without directly risking Mark's own account,
and is worth a scoped future evaluation — but only as an explicit,
individually-acknowledged opt-in (see `sources.risk_acknowledged`), never a
default.

### E. User-session/browser automation

Would require storing and driving a real, logged-in personal Facebook
session (Playwright or similar). This is the option Mark's explicit
instruction rules out as *required* architecture — and for good reason: it
is the highest personal-account-risk option on this list. Facebook actively
fingerprints and challenges non-human browsing patterns, and using a
personal account this way risks that account being restricted or banned —
a cost that falls on Mark personally, not just on the project. If this is
ever built, it must be declared `can_run_unattended=False` in the capability
model regardless of whether it's technically automatable, so the scheduler
structurally cannot pick it up — it would only ever run as a manual,
human-triggered, occasional check, never a background cadence.

### F. Direct scraping

Parsing HTML/undocumented endpoints directly, no third-party provider and
(for Gumtree) no login. Gumtree's listing pages are public, so this is
technically viable there without touching a personal account — the risk is
project/IP-level ToS exposure and ongoing maintenance burden, not personal
account risk, closer in kind to D than to E. Facebook Marketplace mostly
isn't viewable without a session at all, so direct scraping collapses into
needing E for any real Facebook coverage. Gumtree scraping is the more
plausible candidate of this pair if ever revisited, but is still against
Gumtree's stated terms — a deliberate risk-acceptance call for Mark to make
explicitly, not an engineering decision to make quietly.

## Recommendation

- Keep **C (manual-assisted)** as the default for both platforms — already
  shipped, zero risk.
- **B (SearXNG)** is worth a small, cheap experiment for Gumtree specifically
  (not Facebook) — the plumbing already exists from `retailer_price.py`, and
  the risk is negligible even if the yield turns out to be low.
- **D (third-party provider)** is the most realistic path to real Facebook
  Marketplace coverage without risking Mark's own account, and the
  connector model now supports declaring it honestly
  (`is_third_party_provider`, `account_risk="medium"`) — worth scoping
  properly as its own future piece of work, gated by explicit opt-in.
- **E and F are not recommended to build now.** E in particular must never
  be scheduled even if built later — that's a structural rule, not just a
  current-priority call.
- **A remains unavailable** for both platforms; revisit only if either
  platform's official access model changes.
