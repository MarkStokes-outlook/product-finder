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


# --- Suggestion confidence -------------------------------------------------------


def test_suggestion_confidence_single_sighting():
    from product_finder.catalogue import suggestion_confidence

    assert suggestion_confidence(1) == 70.0


def test_suggestion_confidence_climbs_with_corroboration():
    from product_finder.catalogue import suggestion_confidence

    assert suggestion_confidence(2) == 78.0
    assert suggestion_confidence(3) == 86.0


def test_suggestion_confidence_caps_below_100():
    from product_finder.catalogue import suggestion_confidence

    assert suggestion_confidence(20) == 99.0


# --- Suggestion normalisation -----------------------------------------------------


def test_normalize_manufacturer_known_alias_casing():
    from product_finder.catalogue import normalize_manufacturer

    assert normalize_manufacturer("graco") == "Graco"
    assert normalize_manufacturer("GRACO") == "Graco"
    assert normalize_manufacturer("Graco") == "Graco"
    assert normalize_manufacturer("wagner") == "Wagner"
    assert normalize_manufacturer("WAGNER") == "Wagner"
    assert normalize_manufacturer("titan") == "Titan"
    assert normalize_manufacturer("TITAN") == "Titan"
    assert normalize_manufacturer("tritech") == "TriTech"
    assert normalize_manufacturer("TriTech") == "TriTech"


def test_normalize_manufacturer_trims_whitespace():
    from product_finder.catalogue import normalize_manufacturer

    assert normalize_manufacturer("  Makita  ") == "Makita"
    assert normalize_manufacturer("  wagner ") == "Wagner"


def test_normalize_manufacturer_unknown_brand_keeps_original_casing():
    from product_finder.catalogue import normalize_manufacturer

    assert normalize_manufacturer("Makita") == "Makita"
    assert normalize_manufacturer("DeWalt") == "DeWalt"
    assert normalize_manufacturer("") == ""
    assert normalize_manufacturer(None) == ""


def test_is_junk_manufacturer_covers_all_listed_values():
    from product_finder.catalogue import is_junk_manufacturer

    junk_values = [
        "Unbranded", "Unbranded/Generic", "Branded", "Generic", "After Market",
        "Does Not Apply", "Dose Not Apply", "N/A", "NA", "Unknown", "Not specified",
    ]
    for value in junk_values:
        assert is_junk_manufacturer(value) is True, value
        assert is_junk_manufacturer(value.upper()) is True, value
        assert is_junk_manufacturer(f"  {value}  ") is True, value


def test_is_junk_manufacturer_false_for_real_brands():
    from product_finder.catalogue import is_junk_manufacturer

    assert is_junk_manufacturer("Makita") is False
    assert is_junk_manufacturer("Graco") is False


def test_looks_like_seller_name_flags_store_keywords():
    from product_finder.catalogue import looks_like_seller_name

    assert looks_like_seller_name("Tools Direct Store") is True
    assert looks_like_seller_name("PowerToolOutlet") is True
    assert looks_like_seller_name("Acme Trading Ltd") is True
    assert looks_like_seller_name("some_seller_99") is True


def test_looks_like_seller_name_flags_digit_heavy_values():
    from product_finder.catalogue import looks_like_seller_name

    assert looks_like_seller_name("toolshop99123") is True


def test_looks_like_seller_name_false_for_real_brands():
    from product_finder.catalogue import looks_like_seller_name

    assert looks_like_seller_name("Makita") is False
    assert looks_like_seller_name("DeWalt") is False
    assert looks_like_seller_name("Graco") is False
    assert looks_like_seller_name("3M") is False


def test_looks_like_seller_name_respects_allowlist(monkeypatch):
    from product_finder import catalogue

    monkeypatch.setattr(catalogue, "MANUFACTURER_ALLOWLIST", {"tool2000"})
    assert catalogue.looks_like_seller_name("Tool2000") is False
    assert catalogue.looks_like_seller_name("tool2000") is False


def test_normalize_model_null_values():
    from product_finder.catalogue import normalize_model

    for value in ["", "-", "Does Not Apply", "Dose Not Apply", "N/A", "Unknown", "  "]:
        assert normalize_model(value) == "", value
    assert normalize_model(None) == ""


def test_normalize_model_preserves_meaningful_values():
    from product_finder.catalogue import normalize_model

    assert normalize_model("LS0816F/2") == "LS0816F/2"
    assert normalize_model("  DWS520  ") == "DWS520"


def test_normalize_suggestion_rejects_junk_manufacturer():
    from product_finder.catalogue import normalize_suggestion

    assert normalize_suggestion("Does Not Apply", "LS0816F/2") is None
    assert normalize_suggestion("Unbranded", "") is None


def test_normalize_suggestion_rejects_seller_name():
    from product_finder.catalogue import normalize_suggestion

    assert normalize_suggestion("Tools Direct Store", "") is None


def test_normalize_suggestion_accepts_and_normalises_real_brand():
    from product_finder.catalogue import normalize_suggestion

    assert normalize_suggestion("WAGNER", "N/A") == ("Wagner", "")
    assert normalize_suggestion(" Makita ", " LS0816F/2 ") == ("Makita", "LS0816F/2")
