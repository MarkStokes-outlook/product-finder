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

## Zero-bid AUCTION+FIXED_PRICE bug investigation (2026-07-08)

Captured after Mark reported Product Finder showing a Buy It Now price
(£73.50) as "the" price on a listing also classified as a live auction with
£52.70/0 bids on the real eBay page. Swept 61 real zero-bid AUCTION+
FIXED_PRICE listings and 106 real zero-bid pure-AUCTION listings (no BIN)
across 6 search terms to test the hypothesis that `currentBidPrice` might
be absent at zero bids — **it was never absent in any of the 167 samples**.
At zero bids, `currentBidPrice` was confirmed (via 3 real `getItem` detail
checks) to always equal `minimumPriceToBid` exactly — i.e. `currentBidPrice`
already correctly represents "the starting/minimum bid" the moment an
auction has zero bids; there is no separate start-price field needed.

- `search_auction_with_bin_zero_bids.json` — real capture, `item_summary/search`
  for a `["FIXED_PRICE", "AUCTION"]` listing (Corsair CX550 PSU) with
  `bidCount: 0`: `price` (31.00, BIN) and `currentBidPrice` (9.68) both
  present and distinct, confirming the bug-report scenario reproduces with
  zero bids specifically, not just "has some bids".
- `getitem_auction_with_bin_zero_bids.json` — real capture, single-item
  `getItem` for the same listing: `minimumPriceToBid` (9.68) exactly equals
  `currentBidPrice` (9.68) at zero bids; `bidCount: null`, `uniqueBidderCount: 0`.
  No `startPrice`/`auctionInfo`/equivalent field exists anywhere in the full
  key set of this response — checked explicitly, not assumed absent.

**Root cause identified, not yet fixed at fixture-capture time:**
`EbaySource.search()` only ever wrote the BIN-preferring `_price_value()`
fallback to `Listing.price`; it never captured `currentBidPrice` as a
distinct value at all (only `get_item()`, used by the much-less-frequent
auction-close poller, did). A freshly-discovered BIN+auction listing had no
distinct current-bid value anywhere until the tiered poller happened to
reach it — see the implementation notes for the actual fix.
