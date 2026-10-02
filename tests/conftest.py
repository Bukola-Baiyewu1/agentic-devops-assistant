import pytest

from src.state import store
from src.demo import sim


@pytest.fixture(autouse=True)
def clean_state():
    """Reset shared state before every test so they don't leak into each other."""
    store.clear()
    sim.reset()
    yield
    store.clear()
    sim.reset()
