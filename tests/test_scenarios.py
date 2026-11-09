"""The ladder of situations from eval.py, run as tests."""
import pytest

from eval import SCENARIOS, run_scenario


@pytest.mark.parametrize("sc", SCENARIOS, ids=[f"rung{s['rung']}-{s['name']}" for s in SCENARIOS])
def test_scenario(sc):
    r = run_scenario(sc)
    assert r["ok"], r["transcript"]
    assert r["false_promises"] == 0
