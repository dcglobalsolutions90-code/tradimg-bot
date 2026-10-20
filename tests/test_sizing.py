"""Unit tests for signal_core.compute_n against the paper bot's exact formula
(reference/kalshi_multi_live_PAPER.py:84-85):

    openc = sum(p["n"] * p["cost"] for p in ac["pos"]); eq = ac["cash"] + openc
    stake = min(ACCOUNTS[an]["stake"], max(0.0, P["open_cap"] * eq - openc))
    n = int(min(stake // cost, ac["cash"] // (cost + P["fee"])))

These are regression tests: compute_n is a straight port, so this guards against
future edits accidentally changing the arithmetic.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signal_core as sc


def reference_formula(stake, cash, cost, fee, open_value, equity, open_cap=0.5):
    stake_eff = min(stake, max(0.0, open_cap * equity - open_value))
    return int(min(stake_eff // cost, cash // (cost + fee)))


CASES = [
    dict(stake=40.0, cash=1000.0, cost=0.82, fee=0.02, open_value=0.0, equity=1000.0),
    dict(stake=100.0, cash=1000.0, cost=0.95, fee=0.02, open_value=0.0, equity=1000.0),
    dict(stake=100.0, cash=1000.0, cost=0.95, fee=0.02, open_value=480.0, equity=1000.0),  # near the 50% cap
    dict(stake=40.0, cash=5.0, cost=0.90, fee=0.02, open_value=0.0, equity=1000.0),         # cash-starved
    dict(stake=40.0, cash=1000.0, cost=0.96, fee=0.02, open_value=499.0, equity=1000.0),    # cap almost exhausted
    dict(stake=40.0, cash=1000.0, cost=0.96, fee=0.02, open_value=500.0, equity=1000.0),    # cap exactly exhausted -> 0
    dict(stake=4.0, cash=100.0, cost=0.05, fee=0.02, open_value=0.0, equity=100.0),         # cheap contract, small stake
]


def test_compute_n_matches_reference_formula_exactly():
    for c in CASES:
        got = sc.compute_n(c["stake"], c["cash"], c["cost"], c["fee"], c["open_value"], c["equity"])
        want = reference_formula(c["stake"], c["cash"], c["cost"], c["fee"], c["open_value"], c["equity"])
        assert got == want, f"case {c}: compute_n={got} reference={want}"


def test_compute_n_zero_when_cap_exhausted():
    n = sc.compute_n(stake=40.0, cash=1000.0, cost=0.96, fee_per_contract=0.02, open_value=500.0, equity=1000.0)
    assert n == 0


def test_compute_n_never_negative():
    n = sc.compute_n(stake=40.0, cash=1000.0, cost=0.96, fee_per_contract=0.02, open_value=900.0, equity=1000.0)
    assert n >= 0


def test_compute_n_zero_cash():
    n = sc.compute_n(stake=40.0, cash=0.0, cost=0.5, fee_per_contract=0.02, open_value=0.0, equity=1000.0)
    assert n == 0


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception:
            failed += 1
            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
