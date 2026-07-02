from product_finder import grading, scoring
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
