import pytest

import anima


def test_anima_exports():
    """Verify top-level package exports match __all__ definition."""
    for symbol in anima.__all__:
        assert hasattr(anima, symbol), f"Export '{symbol}' missing from anima package"


def test_unknown_attribute_raises():
    """Lazy attribute lookup rejects names outside the public exports."""
    with pytest.raises(AttributeError):
        getattr(anima, "nonexistent_symbol")  # noqa: B009
