import pytest

import animas


def test_anima_exports():
    """Verify top-level package exports match __all__ definition."""
    for symbol in animas.__all__:
        assert hasattr(animas, symbol), f"Export '{symbol}' missing from animas package"


def test_unknown_attribute_raises():
    """Lazy attribute lookup rejects names outside the public exports."""
    with pytest.raises(AttributeError):
        getattr(animas, "nonexistent_symbol")  # noqa: B009
