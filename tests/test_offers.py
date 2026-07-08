from product_finder import offers
from product_finder.models import Listing


# --- detect_offer_support ----------------------------------------------------------


def test_detect_offer_support_true_when_best_offer_present():
    listing = Listing(
        source="ebay", external_id="1", title="x", price=100.0, url="https://x/1",
        buying_options=["FIXED_PRICE", "BEST_OFFER"],
    )
    assert offers.detect_offer_support(listing) is True


def test_detect_offer_support_false_for_plain_fixed_price():
    listing = Listing(
        source="ebay", external_id="2", title="x", price=100.0, url="https://x/2",
        buying_options=["FIXED_PRICE"],
    )
    assert offers.detect_offer_support(listing) is False


def test_detect_offer_support_false_for_auction():
    listing = Listing(
        source="ebay", external_id="3", title="x", price=100.0, url="https://x/3",
        buying_options=["AUCTION"],
    )
    assert offers.detect_offer_support(listing) is False


# --- suggest_offers(): no-offer-support short-circuit ------------------------------


def test_no_offer_support_returns_no_numbers():
    result = offers.suggest_offers(
        listing_price=100.0, reference_price=90.0, supports_offers=False
    )
    assert result.supports_offers is False
    assert result.safe_offer is None
    assert result.normal_offer is None
    assert result.cheeky_offer is None


# --- suggest_offers(): no reference price (low confidence, flat heuristic) -------


def test_no_reference_price_uses_flat_percentages_off_asking():
    result = offers.suggest_offers(listing_price=400.0, reference_price=None)
    assert result.confidence == "low"
    assert result.safe_offer == 360.0  # 400 * 0.90
    assert result.normal_offer == 340.0  # 400 * 0.85
    # low confidence caps the cheeky offer at the "normal" flat discount, not 22.5%
    assert result.cheeky_offer == 340.0
    assert "No reference price yet" in result.explanation


# --- suggest_offers(): priced within/at market ------------------------------------


def test_priced_at_market_uses_flat_percentages_and_shows_range():
    # Listing price equals reference price -> not "above market".
    result = offers.suggest_offers(
        listing_price=350.0, reference_price=350.0, grade="A", verified=True,
    )
    assert result.confidence == "high"
    assert result.safe_offer == 315.0  # 350 * 0.90
    assert result.normal_offer == 297.5  # 350 * 0.85
    assert result.cheeky_offer == round(350.0 * (1 - 0.225), 2)
    assert "Try £" in result.explanation


# --- suggest_offers(): seller pricing above market --------------------------------


def test_priced_above_market_flags_it_in_explanation():
    # Listed at £400, typical used is £350 -> matches the worked example in
    # the brief ("Listed at £400; typical used is £350...").
    result = offers.suggest_offers(
        listing_price=400.0, reference_price=350.0, grade="A", verified=True,
    )
    assert "Listed at £400" in result.explanation
    assert "typical used price is £350" in result.explanation
    assert "Seller is above market" in result.explanation
    # Flat 15%/22.5%/10% off the inflated £400 asking already land at or
    # below the £350 reference here, so the anchor is a no-op in this
    # particular case - see the next test for asking prices close enough to
    # the reference that anchoring actually changes the numbers.
    assert result.normal_offer == 340.0
    assert result.cheeky_offer == round(400.0 * (1 - 0.225), 2)
    assert result.safe_offer == round(400.0 * (1 - 0.10), 2)


def test_priced_above_market_anchoring_pulls_offers_down_to_reference():
    # Asking £420 is only modestly above the £350 reference - flat
    # percentages off asking (10/15/22.5%) would land above the reference
    # for all three tiers, so anchoring must pull each one down.
    result = offers.suggest_offers(
        listing_price=420.0, reference_price=350.0, grade="A", verified=True,
    )
    assert result.normal_offer == 350.0  # min(420*0.85=357, 350)
    assert result.cheeky_offer == round(350.0 * 0.9, 2)  # min(420*0.775=325.5, 315) -> 315
    assert result.safe_offer == round(350.0 * 1.05, 2)  # min(420*0.9=378, 367.5) -> 367.5


# --- suggest_offers(): confidence tiers --------------------------------------------


def test_unclear_grade_forces_low_confidence_and_caps_cheeky_offer():
    result = offers.suggest_offers(
        listing_price=400.0, reference_price=350.0, grade="unknown", verified=False,
    )
    assert result.confidence == "low"
    # Cheeky offer capped at the conservative flat discount off asking
    # (400 * 0.85 = 340), not pushed all the way down toward the reference.
    assert result.cheeky_offer == 340.0


def test_verified_clear_grade_is_high_confidence():
    result = offers.suggest_offers(
        listing_price=350.0, reference_price=350.0, grade="B", verified=True,
    )
    assert result.confidence == "high"


def test_unverified_clear_grade_is_medium_confidence():
    result = offers.suggest_offers(
        listing_price=350.0, reference_price=350.0, grade="A", verified=False,
    )
    assert result.confidence == "medium"


def test_low_seller_confidence_forces_low_even_with_good_evidence():
    result = offers.suggest_offers(
        listing_price=350.0, reference_price=350.0, grade="A", verified=True,
        seller_confidence=0.2,
    )
    assert result.confidence == "low"


def test_low_source_confidence_forces_low_even_with_good_evidence():
    result = offers.suggest_offers(
        listing_price=350.0, reference_price=350.0, grade="A", verified=True,
        source_confidence=0.3,
    )
    assert result.confidence == "low"


def test_seller_and_source_confidence_default_to_neutral_when_unknown():
    # No connector supplies these today - omitting them must not force "low"
    # on its own (that's what the unclear-grade/no-reference paths are for).
    result = offers.suggest_offers(
        listing_price=350.0, reference_price=350.0, grade="A", verified=True,
    )
    assert result.confidence == "high"
