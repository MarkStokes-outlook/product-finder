from product_finder import spec_match


def test_capacity_binds_to_nearest_component_not_just_the_number():
    caps = spec_match.extract("8GB DDR4 RAM 128GB SSD").capacities
    by_component = {c.component: c.value_gb for c in caps}
    assert by_component["ram"] == 8
    assert by_component["storage_ssd"] == 128


def test_vram_and_system_ram_are_distinct_components():
    caps = spec_match.extract("RTX 3080 16GB VRAM").capacities
    assert caps[0].component == "vram"
    assert caps[0].value_gb == 16
    caps2 = spec_match.extract("Laptop 16GB System RAM").capacities
    assert any(c.component == "ram" and c.value_gb == 16 for c in caps2)


def test_ssd_and_hdd_are_distinct_components():
    caps = spec_match.extract("2TB SSD").capacities
    assert caps == [spec_match.Capacity("storage_ssd", 2048.0, "2tb")]
    caps2 = spec_match.extract("2TB HDD").capacities
    assert caps2[0].component == "storage_hdd"


def test_multiplier_capacity_is_totalled():
    caps = spec_match.extract("Corsair 4x32GB DDR5 Kit").capacities
    assert any(c.component == "ram" and c.value_gb == 128 for c in caps)


def test_multiplier_capacity_binds_correctly_even_adjacent_to_bare_total():
    # "128GB (2x64GB)" — the bare total and the multiplier form sit right
    # next to each other; at least one must still bind correctly.
    caps = spec_match.extract("128GB (2x64GB) DDR5 Desktop Memory").capacities
    assert any(c.component == "ram" and c.value_gb == 128 for c in caps)


def test_tech_attribute_families_detected():
    tags = spec_match.extract("32GB DDR5 ECC Registered Server Memory").tech_tags
    assert tags["ram_generation"] == "ddr5"
    assert tags["ram_ecc"] == "ecc"
    assert tags["ram_buffering"] == "registered"


def test_non_ecc_not_confused_with_ecc():
    tags = spec_match.extract("16GB DDR4 Non-ECC Unbuffered Desktop Memory").tech_tags
    assert tags["ram_ecc"] == "non_ecc"
    assert tags["ram_buffering"] == "unbuffered"


def test_sodimm_resolves_to_laptop_form_factor_not_desktop():
    tags = spec_match.extract("8GB DDR4 SO-DIMM Laptop Memory").tech_tags
    assert tags["ram_form_factor"] == "laptop"


def test_bare_dimm_resolves_to_desktop_form_factor():
    tags = spec_match.extract("16GB DDR4 UDIMM Desktop Memory").tech_tags
    assert tags["ram_form_factor"] == "desktop"


def test_category_tags_laptop_model_families():
    cats = spec_match.extract("Dell Latitude 5420 8GB RAM 128GB SSD").categories
    assert "laptop" in cats
    assert "ram" in cats
    assert "storage_ssd" in cats


def test_multi_component_capacity_implies_system_even_without_brand_keyword():
    # No "laptop"/"desktop"/model-family keyword at all — purely the fact
    # that RAM and storage capacities are both bound should be enough.
    cats = spec_match.extract("Acme Widget 8GB RAM 128GB SSD").categories
    assert "multi_component_system" in cats


def test_single_component_listing_is_not_tagged_multi_component():
    cats = spec_match.extract("Kingston 128GB DDR5 RAM Kit").categories
    assert "multi_component_system" not in cats


# --- compare() -----------------------------------------------------------------


def test_compare_flags_capacity_mismatch_for_same_component():
    wanted = spec_match.extract("128GB DDR5 RAM")
    listing = spec_match.extract("Dell Latitude 8GB DDR4 RAM 128GB SSD")
    conflicts = spec_match.compare(wanted, listing)
    kinds = {c.kind for c in conflicts}
    assert spec_match.CONFLICT_CAPACITY in kinds
    capacity_conflict = next(c for c in conflicts if c.kind == spec_match.CONFLICT_CAPACITY)
    assert "128GB" in capacity_conflict.message
    assert "8GB" in capacity_conflict.message


def test_compare_flags_generation_mismatch():
    wanted = spec_match.extract("128GB DDR5 RAM")
    listing = spec_match.extract("Dell Latitude 8GB DDR4 RAM 128GB SSD")
    conflicts = spec_match.compare(wanted, listing)
    assert any(c.kind == spec_match.CONFLICT_SPEC and "ddr5" in c.message and "ddr4" in c.message
               for c in conflicts)


def test_compare_flags_category_mismatch_component_vs_system():
    wanted = spec_match.extract("128GB DDR5 RAM")
    listing = spec_match.extract("Dell Latitude 8GB DDR4 RAM 128GB SSD")
    conflicts = spec_match.compare(wanted, listing)
    assert any(c.kind == spec_match.CONFLICT_CATEGORY for c in conflicts)


def test_compare_flags_category_mismatch_between_different_components():
    # SSD vs HDD: not a "same component, different value" capacity conflict,
    # but a real category mismatch (different component entirely).
    wanted = spec_match.extract("2TB SSD")
    listing = spec_match.extract("Seagate 2TB HDD")
    conflicts = spec_match.compare(wanted, listing)
    assert any(c.kind == spec_match.CONFLICT_CATEGORY for c in conflicts)
    assert not any(c.kind == spec_match.CONFLICT_CAPACITY for c in conflicts)


def test_compare_flags_vram_vs_system_ram_mismatch():
    wanted = spec_match.extract("16GB VRAM Graphics Card")
    listing = spec_match.extract("Laptop 16GB System RAM")
    conflicts = spec_match.compare(wanted, listing)
    assert any(c.kind == spec_match.CONFLICT_CATEGORY for c in conflicts)


def test_compare_no_conflicts_for_genuine_match():
    wanted = spec_match.extract("128GB DDR5 RAM 4x32GB DDR5")
    listing = spec_match.extract("Corsair Vengeance 128GB (4x32GB) DDR5 6000MHz Desktop Memory")
    assert spec_match.compare(wanted, listing) == []


def test_compare_silence_is_not_a_conflict():
    # Listing doesn't mention DDR generation at all — silence, not
    # disagreement, so no spec conflict should fire for ram_generation.
    wanted = spec_match.extract("128GB DDR5 RAM")
    listing = spec_match.extract("128GB RAM Kit, excellent condition")
    conflicts = spec_match.compare(wanted, listing)
    assert not any(c.kind == spec_match.CONFLICT_SPEC for c in conflicts)


def test_compare_empty_wanted_or_listing_yields_no_conflicts():
    empty = spec_match.extract("")
    wanted = spec_match.extract("128GB DDR5 RAM")
    assert spec_match.compare(wanted, empty) == []
    assert spec_match.compare(empty, wanted) == []


def test_capacity_tolerance_absorbs_binary_decimal_rounding():
    # 1TB (1024GB) vs "1000GB" marketing figure for the same real product —
    # should not be treated as a genuine capacity contradiction.
    wanted = spec_match.extract("1TB SSD")
    listing = spec_match.extract("1000GB SSD, boxed")
    conflicts = spec_match.compare(wanted, listing)
    assert not any(c.kind == spec_match.CONFLICT_CAPACITY for c in conflicts)
