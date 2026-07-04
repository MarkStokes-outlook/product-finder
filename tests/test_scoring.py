from product_finder import grading, price_trend, scoring
from product_finder.catalogue import Product
from product_finder.config import ItemConfig
from product_finder.models import Listing


def make_item(**overrides):
    defaults = dict(
        name="Track Saw",
        terms=["track saw"],
        max_price=400,
        normal_price=500,
        target_deal_price=300,
        priority="high",
        exclude_terms=["toy"],
    )
    defaults.update(overrides)
    return ItemConfig(**defaults)


def make_listing(title, price, description=""):
    return Listing(
        source="ebay",
        external_id="x1",
        title=title,
        price=price,
        url="https://example.com/1",
        description=description,
    )


def test_warning_flags():
    flags = scoring.warning_flags("Faulty saw, spares or repairs, no charger")
    assert "faulty" in flags
    assert "spares or repairs" in flags
    assert "no charger" in flags
    assert scoring.warning_flags("Good condition, fully working") == []


# --- Negation handling (see grading.phrase_present, shared by both modules) -----


def test_warning_flags_ignores_negated_faults():
    # The exact roadmap example, plus the other named cases — none of these
    # are real faults, just the listing explicitly ruling them out.
    assert scoring.warning_flags("Monitor, no dead pixels, excellent condition") == []
    assert scoring.warning_flags("Toolbox, no cracks, mint condition") == []
    assert scoring.warning_flags("Case has no damage, as new") == []
    assert scoring.warning_flags("Not scratched, boxed") == []


def test_warning_flags_still_catches_real_faults_alongside_negated_ones():
    # A listing can truthfully rule out one fault while reporting another —
    # negation must not become a blanket suppressor for the whole text.
    flags = scoring.warning_flags("No dead pixels, but screen is cracked")
    assert "broken" in flags
    assert "not working" not in flags


def test_warning_flags_terms_with_embedded_negation_still_fire():
    # "not working" / "no charger" / "no battery" are themselves the fault
    # phrase, not something an external negator cancels out.
    flags = scoring.warning_flags("Saw not working, no charger, no battery included")
    assert "not working" in flags
    assert "no charger" in flags
    assert "missing battery" in flags


def test_margins():
    assert scoring.margins(300, 500) == (200.0, 40.0)
    assert scoring.margins(300, None) == (0.0, 0.0)


def test_good_deal_beats_false_bargain():
    item = make_item()
    good = scoring.evaluate(make_listing("Makita track saw, excellent condition", 250), item)
    trap = scoring.evaluate(make_listing("Makita track saw, faulty, spares or repairs", 100), item)
    assert good.deal_score > trap.deal_score
    assert trap.grade == grading.SPARES
    assert trap.flags


def test_under_target_detected():
    item = make_item()
    ev = scoring.evaluate(make_listing("Track saw good condition", 290), item)
    assert ev.under_target is True
    over = scoring.evaluate(make_listing("Track saw good condition", 350), item)
    assert over.under_target is False


def test_false_bargain_heuristic():
    assert scoring.is_likely_false_bargain(100, 500, ["faulty"]) is True
    assert scoring.is_likely_false_bargain(100, 500, []) is False
    assert scoring.is_likely_false_bargain(400, 500, ["faulty"]) is False


def test_grade_ordering():
    item = make_item()
    a = scoring.evaluate(make_listing("Track saw, like new", 300), item)
    c = scoring.evaluate(make_listing("Track saw, well used and rough", 300), item)
    assert a.deal_score > c.deal_score


def test_exclude_terms():
    item = make_item()
    assert scoring.excluded(make_listing("Toy track saw for kids", 10), item) is True
    assert scoring.excluded(make_listing("Makita track saw", 250), item) is False


def make_product(**overrides):
    defaults = dict(
        id=1, item_id=1, manufacturer="Makita", model="SP6000",
        match_terms=["makita sp6000"], msrp=None, typical_new_price=None,
        typical_used_price=None, target_deal_price=None,
    )
    defaults.update(overrides)
    return Product(**defaults)


def make_auction_listing(title, current_bid):
    return Listing(
        source="ebay", external_id="a1", title=title, price=current_bid,
        url="https://example.com/a1", buying_options=["AUCTION"],
    )


# --- Product catalogue overrides ----------------------------------------------


def test_effective_prices_falls_back_to_item_with_no_product():
    item = make_item()
    assert scoring.effective_prices(item, None) == (500, 300, None)


def test_effective_prices_uses_product_when_set():
    item = make_item()  # normal_price=500, target_deal_price=300
    product = make_product(typical_new_price=180, target_deal_price=120)
    assert scoring.effective_prices(item, product) == (180, 120, None)


def test_effective_prices_partial_override_falls_back_per_field():
    item = make_item()  # normal_price=500, target_deal_price=300
    product = make_product(typical_new_price=180, target_deal_price=None)
    assert scoring.effective_prices(item, product) == (180, 300, None)


def test_effective_prices_falls_back_to_msrp_before_item():
    item = make_item()  # normal_price=500
    product = make_product(msrp=600, typical_new_price=None)
    normal_price, _, _ = scoring.effective_prices(item, product)
    assert normal_price == 600


def test_effective_prices_surfaces_typical_used_price():
    item = make_item()
    product = make_product(typical_used_price=100)
    assert scoring.effective_prices(item, product) == (500, 300, 100)


def test_product_price_prevents_cheap_item_being_scored_as_amazing_deal():
    # A budget own-brand saw at £150 is a fair price for a £180 tool, not a
    # bargain against the item's blended £500 "normal" price.
    item = make_item()
    budget_product = make_product(
        id=2, manufacturer="Own Brand", model="", match_terms=["own brand"],
        typical_new_price=180, target_deal_price=None,
    )
    without_catalogue = scoring.evaluate(make_listing("Own Brand mitre saw, good condition", 150), item)
    with_catalogue = scoring.evaluate(
        make_listing("Own Brand mitre saw, good condition", 150), item, budget_product
    )
    assert without_catalogue.deal_score > with_catalogue.deal_score
    assert with_catalogue.margin_pct < without_catalogue.margin_pct


def test_product_normal_price_used_for_margin():
    item = make_item()
    product = make_product(typical_new_price=250)
    ev = scoring.evaluate(make_listing("Makita SP6000 track saw, boxed", 150), item, product)
    assert ev.margin_abs == 100.0
    assert ev.margin_pct == 40.0


# --- Typical used-price sanity check --------------------------------------------


def test_price_above_typical_used_price_is_flagged_and_penalised():
    item = make_item()
    # New price £200 (25% margin at £150) but typical used is only £100 —
    # £150 is a bad deal even though it looks like a saving vs. new.
    product = make_product(typical_new_price=200, typical_used_price=100)
    listing = make_listing("Makita SP6000 track saw, good condition", 150)
    ev = scoring.evaluate(listing, item, product)
    assert "above typical used price" in ev.flags

    cheap_product = make_product(id=3, typical_new_price=200, typical_used_price=100)
    cheap_listing = make_listing("Makita SP6000 track saw, good condition", 90)
    cheap_ev = scoring.evaluate(cheap_listing, item, cheap_product)
    assert "above typical used price" not in cheap_ev.flags
    assert cheap_ev.deal_score > ev.deal_score


def test_deal_score_penalises_price_above_typical_used():
    base = scoring.deal_score(150, 200, None, grading.GRADE_B, [])
    with_used_price = scoring.deal_score(150, 200, None, grading.GRADE_B, [], typical_used_price=100)
    assert with_used_price < base


# --- Live auction handling -------------------------------------------------------


def test_is_live_auction_true_for_auction_buying_option():
    assert scoring.is_live_auction(make_auction_listing("Makita drill", 5.0)) is True


def test_is_live_auction_false_for_fixed_price():
    listing = Listing(
        source="ebay", external_id="f1", title="Makita drill", price=50.0,
        url="https://example.com/f1", buying_options=["FIXED_PRICE"],
    )
    assert scoring.is_live_auction(listing) is False


def test_live_auction_flagged_and_never_double_flagged_with_used_price():
    item = make_item()
    product = make_product(typical_new_price=200, typical_used_price=100)
    # Priced (as a current bid) well above typical used price too — but the
    # auction flag should win; the number isn't trustworthy either way.
    listing = make_auction_listing("Makita SP6000 track saw", 150)
    ev = scoring.evaluate(listing, item, product)
    assert "live auction" in ev.flags
    assert "above typical used price" not in ev.flags


# --- Multi-item / price-range listing handling -----------------------------------


def test_is_multi_item_detects_price_range_in_title():
    assert scoring.is_multi_item_or_price_range(make_listing("Makita drills, various models £95 - £299", 95)) is True
    assert scoring.is_multi_item_or_price_range(make_listing("Bosch saws £1,299-£1,499", 1299)) is True
    assert scoring.is_multi_item_or_price_range(make_listing("Tool lot, £50 to £120", 50)) is True


def test_is_multi_item_detects_bundle_language():
    assert scoring.is_multi_item_or_price_range(make_listing("Job lot of power tools", 80)) is True
    assert scoring.is_multi_item_or_price_range(make_listing("Bundle of 3 drills, choose from", 60)) is True


def test_is_multi_item_false_for_ordinary_single_item_listing():
    assert scoring.is_multi_item_or_price_range(make_listing("Makita LS0815FL mitre saw, boxed", 250)) is False


def test_is_multi_item_ignores_description_only_ranges():
    # Deliberate scoping decision (see module comment): a range spelled out
    # only in the description, not the title, is a known, accepted miss —
    # this guards against "was £299, now £95" single-item markdown framing
    # in descriptions being misread as a multi-item price range.
    listing = make_listing(
        "Makita LS0815FL mitre saw, boxed", 95,
        description="RRP was £299, reduced to £95 for quick sale",
    )
    assert scoring.is_multi_item_or_price_range(listing) is False


def test_multi_item_listing_flagged_and_never_under_target():
    item = make_item()  # target_deal_price=300
    listing = make_listing("Makita drills, various models £95 - £299", 95)
    ev = scoring.evaluate(listing, item)
    assert "multiple items / price range" in ev.flags
    assert ev.under_target is False  # £95 <= £300 target, but ambiguous — never a confirmed match


def test_ordinary_listing_unaffected_by_multi_item_check():
    item = make_item()
    ev = scoring.evaluate(make_listing("Makita track saw, good condition", 290), item)
    assert "multiple items / price range" not in ev.flags
    assert ev.under_target is True


# --- Used-price trend adjustment (see price_trend.py) ---------------------------


def test_deal_score_applies_trend_adjustment():
    base = scoring.deal_score(150, 200, None, grading.GRADE_B, [])
    falling = scoring.deal_score(
        150, 200, None, grading.GRADE_B, [], price_trend_pct=-10.0, price_trend_confidence=0.5
    )
    rising = scoring.deal_score(
        150, 200, None, grading.GRADE_B, [], price_trend_pct=10.0, price_trend_confidence=0.5
    )
    assert falling < base < rising


def test_deal_score_trend_adjustment_is_capped():
    base = scoring.deal_score(150, 200, None, grading.GRADE_B, [])
    extreme = scoring.deal_score(
        150, 200, None, grading.GRADE_B, [], price_trend_pct=-500.0, price_trend_confidence=1.0
    )
    assert base - extreme == price_trend.MAX_SCORE_ADJUSTMENT


def test_deal_score_no_trend_data_leaves_score_unchanged():
    base = scoring.deal_score(150, 200, None, grading.GRADE_B, [])
    no_trend = scoring.deal_score(
        150, 200, None, grading.GRADE_B, [], price_trend_pct=None, price_trend_confidence=0.0
    )
    assert base == no_trend


def test_evaluate_reads_trend_from_matched_product():
    item = make_item()
    listing = make_listing("Makita SP6000 track saw, good condition", 150)
    steady_product = make_product(typical_new_price=200)
    falling_product = make_product(
        id=2, typical_new_price=200, price_trend_pct=-15.0, price_trend_confidence=1.0
    )
    steady = scoring.evaluate(listing, item, steady_product)
    falling = scoring.evaluate(listing, item, falling_product)
    assert falling.deal_score < steady.deal_score


def test_evaluate_with_no_product_applies_no_trend_adjustment():
    item = make_item()
    ev = scoring.evaluate(make_listing("Track saw good condition", 290), item)
    expected = scoring.deal_score(
        290, item.normal_price, item.target_deal_price, ev.grade, ev.flags,
        title="Track saw good condition",
    )
    assert ev.deal_score == expected


# --- 2026-07-04 recalibration: inverted-U margin + implausible-price gate -------


def test_margin_term_rises_to_plateau():
    assert scoring.margin_term(25, verified=False) == 25 * scoring.MARGIN_PER_PCT
    assert (
        scoring.margin_term(scoring.MARGIN_PLATEAU_START_PCT, verified=False)
        == scoring.MARGIN_PLATEAU_SCORE
    )


def test_margin_term_verified_deep_discount_keeps_plateau():
    # A trusted reference price (catalogue product) means a deep discount is
    # a deep discount, not evidence of a wrong-product match.
    assert scoring.margin_term(90, verified=True) == scoring.MARGIN_PLATEAU_SCORE


def test_margin_term_decays_for_unverified_deep_discounts():
    plateau = scoring.margin_term(60, verified=False)
    decayed = scoring.margin_term(80, verified=False)
    deep = scoring.margin_term(95, verified=False)
    assert plateau == scoring.MARGIN_PLATEAU_SCORE
    assert deep < decayed < plateau
    assert scoring.margin_term(100, verified=False) == scoring.MARGIN_MIN_SCORE


def test_unverified_extreme_discount_scores_below_moderate_discount():
    # The inverted U end-to-end: a plausible half-price listing must outrank
    # an "88% off" one — at that depth it's almost never really the item.
    item = make_item()  # normal_price=500
    moderate = scoring.evaluate(make_listing("Track saw, good condition", 225), item)
    extreme = scoring.evaluate(make_listing("Track saw rail cover, good condition", 62), item)
    assert moderate.deal_score > extreme.deal_score


def test_implausible_price_flagged_and_never_under_target():
    # £4 against a £500 normal price is an accessory, not the item — flag it
    # (which keeps it out of the spotlight via the existing flagged filter)
    # and never treat its price as meeting the item's target.
    item = make_item()  # normal_price=500, target_deal_price=300
    ev = scoring.evaluate(make_listing("Track saw blade screw M8", 4.0), item)
    assert scoring.FLAG_IMPLAUSIBLE_PRICE in ev.flags
    assert ev.under_target is False


def test_implausible_price_never_fires_for_product_matched_listing():
    item = make_item()
    product = make_product(typical_new_price=500)
    ev = scoring.evaluate(make_listing("Makita SP6000 track saw, boxed", 40.0), item, product)
    assert scoring.FLAG_IMPLAUSIBLE_PRICE not in ev.flags


def test_deal_score_cannot_saturate_at_100():
    # Regression guard for the old saturation: a perfect-storm listing (clean,
    # grade A, under target, plateau margin, max favourable trend) must still
    # land below 100 so the top of the range keeps discriminating.
    score = scoring.deal_score(
        200, 500, 300, grading.GRADE_A, [],
        title="Makita SP6000 track saw excellent condition",
        price_trend_pct=100.0, price_trend_confidence=1.0, verified=True,
    )
    assert score < 100


def test_deal_score_is_priority_blind():
    # Priority is how much the operator wants the item, not how good the deal
    # is — identical listings under high- and low-priority items score alike.
    listing_title = "Track saw, good condition"
    high = scoring.evaluate(make_listing(listing_title, 250), make_item(priority="high"))
    low = scoring.evaluate(make_listing(listing_title, 250), make_item(priority="low"))
    assert high.deal_score == low.deal_score


def test_live_auction_never_beats_fixed_price_on_score():
    item = make_item()
    product = make_product(typical_new_price=200, typical_used_price=100)
    auction = scoring.evaluate(make_auction_listing("Makita SP6000 track saw", 5.0), item, product)
    fixed = scoring.evaluate(
        Listing(source="ebay", external_id="f2", title="Makita SP6000 track saw", price=90.0,
                url="https://example.com/f2", buying_options=["FIXED_PRICE"]),
        item, product,
    )
    assert fixed.deal_score > auction.deal_score
