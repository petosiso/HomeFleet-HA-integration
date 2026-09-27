"""Use the real Home Assistant test framework, separate from portable doubles."""

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Allow HomeFleet to load from custom_components."""
    yield
