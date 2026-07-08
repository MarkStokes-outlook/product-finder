# eBay Browse API fixtures — provenance

Captured 2026-07-08 via this project's own real eBay developer credentials
(`config.yaml` / `source_settings`), using the exact endpoints and filters
`EbaySource` already uses in production (`item_summary/search` and
single-item `getItem`). This exists because prior test coverage used
hand-written inline dicts, not real API shapes — these files are the actual
evidence for `currentBidPrice`/`bidCount`/`buyingOptions`/
`estimatedAvailabilities` field presence, not a guess at eBay's schema.

Every file has been sanitised before being committed: seller username,
feedback score, image URLs, item IDs, `itemWebUrl`/`itemHref`, and the
`itemLocation.city` are replaced with clearly-fake placeholders. All other
fields (prices, bid counts, categories, condition, dates, buying options,
localized aspects, shipping/tax/return terms) are untouched real API output.

- `search_auction_no_bin.json` — real capture, `item_summary/search` response
  for a live auction (RTX 3080) with no Buy It Now: `price` absent,
  `currentBidPrice`/`bidCount`/`buyingOptions: ["AUCTION"]` present.
- `search_fixed_price.json` — real capture, `item_summary/search` response
  for an ordinary `FIXED_PRICE` listing (mitre saw), for contrast.
- `getitem_auction_active.json` — real capture, single-item `getItem` for the
  same live auction above, `estimatedAvailabilities[0].estimatedAvailabilityStatus
  == "IN_STOCK"` — i.e. captured *before* its end time.
- `getitem_auction_ended.json` — **not a live capture**. Derived from
  `getitem_auction_active.json` by manually flipping
  `estimatedAvailabilityStatus` to `OUT_OF_STOCK` (and zeroing the quantity
  fields). This item's real `itemEndDate` (2026-07-08T17:00:01Z) was ~5.5
  hours away at capture time, too far off to wait for a genuine live close
  in-session. The `OUT_OF_STOCK`-flip-means-ended behaviour this represents
  was verified live in a prior session (see
  `docs/implementation-notes/` deal-scoring notes and `auction_watch.py`
  docstring) — this fixture documents that already-verified shape, it does
  not re-derive it from documentation alone.

- `search_auction_with_bin.json` — real capture, `item_summary/search` for a
  listing with **both** `AUCTION` and `FIXED_PRICE` (PS5 console): `price`
  (229.50, the Buy It Now price) and `currentBidPrice` (156.70, the current
  bid) both present simultaneously and distinctly — confirms the two are
  independent fields, not a fallback of one for the other, when both buying
  options are active.
- `getitem_auction_with_bin.json` — real capture, single-item `getItem` for
  the same listing. `shippingOptions[0].shippingCost.value` confirmed present
  here (`"5.88"`) — the real shape used for auction-snapshot shipping price.
- `search_best_offer.json` — real capture, `item_summary/search` for a
  `["FIXED_PRICE", "BEST_OFFER"]` listing — evidence that `BEST_OFFER` is a
  real, distinctly-appearing value in `buyingOptions` (used by offer
  intelligence work).

**Confirmed absent from every real capture above** (checked the full key set
of both `getItem` responses, not just the fields we expected):
no `watchCount`/`viewCount`/equivalent field exists anywhere in eBay's Browse
API `item_summary` or `getItem` response. Auction snapshot fields for these
are always recorded as `None`/unknown with provenance, never guessed.
