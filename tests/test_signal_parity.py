"""Signal parity test (BUILD_BRIEF.md test #1).

reference/paper_history.jsonl is a permanent, append-only record of every bet the
paper bot actually placed, across several rule/size configurations over its
runtime (see the spec PDF's change log: minute-3 dropped, D/E/F reset, sizes
changed twice). It records the z, rule, side, cost, size, fee and outcome of each
settled bet, but NOT the raw Coinbase candles or market snapshot that produced
that z -- so this test cannot re-derive z from scratch and replay the whole
pipeline. What it CAN do, and does, is check every decision invariant the ported
signal_core functions are responsible for, against every one of these real
historical decisions:

  1. side was chosen from z alone (YES iff z > 0), never the market's odds.
  2. the bet's |z| met its rule's minimum sigma.
  3. the fill price was inside the 3-97 cent price limit.
  4. the paper bot's own fee/pnl arithmetic is what we think it is (a cross-check,
     not of signal_core, but of our understanding of the reference implementation).

If any of these ever fails, either signal_core.py has drifted from the paper
bot, or the paper bot's behavior changed in a way this file doesn't know about
yet -- both are worth stopping and looking at.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signal_core as sc

HIST = Path(__file__).resolve().parent.parent / "reference" / "paper_history.jsonl"

# The minute-3 rule (1.25 sigma) was dropped before this build and is NOT in
# signal_core.RULES, but old bets using it are still in the permanent history file.
HISTORICAL_RULE_THRESHOLDS = {**sc.RULES, 3: 1.25}


def _load_history():
    recs = []
    with HIST.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def test_history_file_is_nonempty():
    recs = _load_history()
    assert len(recs) > 100, "expected the reference history to have a meaningful number of settled bets"


def test_side_always_matches_sign_of_z_never_odds():
    for r in _load_history():
        expected = sc.decide_side(r["z"])
        assert r["side"] == expected, f"record {r} used side={r['side']} but z={r['z']} implies {expected}"


def test_every_bet_met_its_rule_threshold():
    for r in _load_history():
        rule = r["rule"]
        threshold = HISTORICAL_RULE_THRESHOLDS.get(rule)
        assert threshold is not None, f"unrecognized rule {rule} in history record {r}"
        assert abs(r["z"]) >= threshold - 1e-9, f"record {r} fired rule {rule} (needs >= {threshold}) with |z|={abs(r['z'])}"


def test_every_bet_respected_the_price_limit():
    for r in _load_history():
        assert sc.price_ok(r["cost"]), f"record {r} had cost {r['cost']} outside [{sc.PRICE_LO}, {sc.PRICE_HI}]"


def test_fee_and_pnl_arithmetic_matches_reference_formula():
    """Cross-check of the paper bot's own formula (spec PDF section 6), not of
    signal_core -- confirms our reading of the reference implementation is right
    before we rely on it anywhere else.
    """
    for r in _load_history():
        expected_fee = round(0.02 * r["n"], 10)
        assert abs(r["fee"] - expected_fee) < 1e-6, f"record {r}: fee {r['fee']} != 0.02*n={expected_fee}"
        if r["win"]:
            expected_pnl = r["n"] * (1 - r["cost"]) - r["fee"]
        else:
            expected_pnl = -r["n"] * r["cost"] - r["fee"]
        assert abs(r["pnl"] - expected_pnl) < 1e-6, f"record {r}: pnl {r['pnl']} != expected {expected_pnl}"


def test_rule_fires_matches_threshold_logic_for_every_minute_in_window():
    # Direct unit check of signal_core.rule_fires against the same thresholds,
    # independent of the history file, for every minute at 0.05 resolution.
    for rule, threshold in sc.RULES.items():
        for tenth in range(0, 200):
            minute = tenth * 0.05
            in_window = rule <= minute < rule + 0.5
            fires_at_threshold = sc.rule_fires(minute, threshold, rule)
            fires_below = sc.rule_fires(minute, threshold - 0.01, rule)
            assert fires_at_threshold == in_window
            assert fires_below is False


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
