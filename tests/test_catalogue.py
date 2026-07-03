from product_finder.catalogue import Product, match


def make_product(**overrides):
    defaults = dict(
        id=1,
        item_id=1,
        manufacturer="Makita",
        model="",
        match_terms=["makita"],
        msrp=None,
        typical_new_price=None,
        typical_used_price=None,
        target_deal_price=None,
        archived=False,
    )
    defaults.update(overrides)
    return Product(**defaults)


def test_no_products_no_match():
    assert match("Makita mitre saw", []) is None


def test_simple_manufacturer_match():
    makita = make_product()
    assert match("Makita mitre saw, good condition", [makita]) is makita


def test_no_match_when_term_absent():
    makita = make_product()
    assert match("DeWalt mitre saw", [makita]) is None


def test_word_boundary_avoids_partial_match():
    # "saw" shouldn't match inside "sawdust".
    saw = make_product(manufacturer="Saw Co", match_terms=["saw"])
    assert match("Sawdust collector bag", [saw]) is None


def test_case_insensitive():
    makita = make_product()
    assert match("MAKITA MITRE SAW", [makita]) is makita


def test_most_specific_term_wins_over_manufacturer_only():
    makita_generic = make_product(id=1, match_terms=["makita"])
    makita_ls1019l = make_product(id=2, model="LS1019L", match_terms=["makita ls1019l"])
    products = [makita_generic, makita_ls1019l]
    result = match("Makita LS1019L mitre saw, boxed", products)
    assert result is makita_ls1019l


def test_sku_alone_beats_manufacturer_alone():
    makita_generic = make_product(id=1, match_terms=["makita"])
    ls1019l_only = make_product(id=2, model="LS1019L", manufacturer="Makita", match_terms=["ls1019l"])
    products = [makita_generic, ls1019l_only]
    result = match("Makita LS1019L mitre saw", products)
    assert result is ls1019l_only


def test_matches_description_not_just_title():
    # Title only says the generic item name; the model is in the description.
    ls1019l = make_product(model="LS1019L", match_terms=["ls1019l"])
    products = [ls1019l]
    # `match()` takes raw text — callers pass listing.text (title + description).
    text = "Mitre saw for sale " + "Model: LS1019L, barely used"
    assert match(text, products) is ls1019l


def test_archived_product_never_matches():
    archived = make_product(archived=True)
    assert match("Makita mitre saw", [archived]) is None


def test_blank_match_terms_ignored():
    product = make_product(match_terms=["", "  ", "makita"])
    assert match("Makita mitre saw", [product]) is product


def test_tie_break_keeps_first_defined():
    first = make_product(id=1, match_terms=["makita ls1019l"])
    second = make_product(id=2, match_terms=["makita ls1019l"])
    assert match("Makita LS1019L", [first, second]) is first
