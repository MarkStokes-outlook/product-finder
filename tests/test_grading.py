from product_finder import grading


def test_grade_a():
    assert grading.classify("Makita SP6000 track saw — brand new, boxed") == grading.GRADE_A
    assert grading.classify("Festool TS55, barely used, immaculate") == grading.GRADE_A
    assert grading.classify("DeWalt mitre saw like new") == grading.GRADE_A


def test_grade_b():
    assert grading.classify("Makita mitre saw, good condition, fully working") == grading.GRADE_B
    assert grading.classify("Used Bosch plunge saw, tested") == grading.GRADE_B


def test_grade_c():
    assert grading.classify("Track saw, well used and tatty but works") == grading.GRADE_C
    assert grading.classify("Mitre saw, heavy use, damaged case") == grading.GRADE_C


def test_spares_repair():
    assert grading.classify("Festool TS55 spares or repairs") == grading.SPARES
    assert grading.classify("DeWalt saw — faulty, not working") == grading.SPARES
    assert grading.classify("Makita saw untested, sold as seen") == grading.SPARES
    assert grading.classify("Parts only, no power") == grading.SPARES


def test_spares_beats_a():
    # A faulty listing dressed up as "like new" is still spares/repair.
    assert grading.classify("Like new but faulty, spares or repairs") == grading.SPARES


def test_unknown():
    assert grading.classify("Makita SP6000") == grading.UNKNOWN
    assert grading.classify("") == grading.UNKNOWN


def test_word_boundaries():
    # "new" must not match inside other words or place names.
    assert grading.classify("Saw collection from Newcastle") == grading.UNKNOWN


# --- Negation handling ------------------------------------------------------------


def test_negated_fault_terms_do_not_trigger_spares_or_c_grade():
    # "damaged case" and "faulty" are real fault terms — negated, they must
    # not count as evidence either way.
    assert grading.classify("Mitre saw, not faulty, no damaged case, good condition") == grading.GRADE_B


def test_negation_scoped_to_current_sentence():
    # "no" here is within the 3-word window of "faulty" by raw word count
    # alone — only stopping the scan at the sentence boundary (the full
    # stop) keeps this correctly classified as a real fault, not negated.
    assert grading.classify("No issues found. Faulty trigger switch.") == grading.SPARES


def test_negation_does_not_suppress_terms_that_embed_their_own_negator():
    # "not working" / "no power" are themselves the fault phrase, not a
    # negation of some other term — must still classify as spares/repair.
    assert grading.classify("Saw is not working, no power") == grading.SPARES
    assert grading.classify("Doesn't work, sold as seen") == grading.SPARES


def test_negated_positive_claim_falls_through_to_next_grade():
    # "not brand new" correctly fails to count as an A-grade claim, but a
    # separate, true B-grade claim in the same text still applies.
    assert grading.classify("Not brand new but still great condition") == grading.GRADE_B


def test_phrase_present_negation_window_is_bounded():
    # A negator far outside the 3-word window shouldn't reach a later fault.
    assert grading.phrase_present("no idea why but this is faulty and untested", "faulty") is True
