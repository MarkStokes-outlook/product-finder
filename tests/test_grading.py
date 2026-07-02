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
