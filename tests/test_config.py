from shadow.config import CATEGORIES


def test_three_categories_configured():
    assert set(CATEGORIES) == {"coffee_shop", "car_repair", "florist"}
    for c in CATEGORIES.values():
        assert c.google_types and c.queries and c.nace_codes
