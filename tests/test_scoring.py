from product_finder import grading, scoring
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
