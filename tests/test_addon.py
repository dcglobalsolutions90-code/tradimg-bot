"""Add-on buy feature: per explicit user request, once every eligible sleeve
already holds a position on a ticker+rule and the signal is still firing, if
the price has gotten cheaper than the position's own average entry cost, add
to the position once (not every further price tick), sized off
addon_stake_pct_of_available (not the original entry's total_stake_pct_of_balance).
Uses tests/fakes.py's FakeClient -- no real network calls.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kalshi_live as kl
from fakes import FakeClient


def _redirect_files(tmp_dir):
    kl.LOG_PATH = tmp_dir / "live_log.jsonl"
    kl.HIST_PATH = tmp_dir / "live_history.jsonl"
    kl.STATE_PATH = tmp_dir / "live_state.json"


def _total_contracts(s, ticker, rule):
    return sum(sum(pos["sleeve_contracts"].values())
               for pos in s["positions"].values() if pos["ticker"] == ticker and pos["rule"] == rule)


def test_addon_fires_once_when_price_improves(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    common = dict(client=client, s=s, cfg=cfg, series="KXBTC15M", ticker="KXBTC15M-TEST",
                  open_t=now - 4 * 60, close_t=now + 600, rule=4, side="yes", z=2.0,
                  live_enabled=False, real_cash=1000.0, real_value=1000.0, exchange_index=0, price_str=None)

    kl.process_rule_signal(cost=0.93, **common)
    n_after_entry = _total_contracts(s, "KXBTC15M-TEST", 4)
    assert n_after_entry > 0, "initial entry should have bought something"

    # Price gets cheaper -- should add on, once.
    kl.process_rule_signal(cost=0.80, **common)
    n_after_addon = _total_contracts(s, "KXBTC15M-TEST", 4)
    assert n_after_addon > n_after_entry, "a cheaper price on an open position should trigger an add-on buy"

    log_lines = kl.LOG_PATH.read_text().splitlines()
    assert any('"type": "addon_decision"' in l for l in log_lines)
    assert "KXBTC15M-TEST|4" in s["addon_seen"]

    # Price gets even cheaper still -- must NOT add on again (one add-on per position).
    kl.process_rule_signal(cost=0.70, **common)
    n_after_second_drop = _total_contracts(s, "KXBTC15M-TEST", 4)
    assert n_after_second_drop == n_after_addon, "add-on must only fire once per position, not on every further drop"
    log_lines = kl.LOG_PATH.read_text().splitlines()
    assert sum('"type": "addon_decision"' in l for l in log_lines) == 1


def test_addon_does_not_fire_without_price_improvement(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    common = dict(client=client, s=s, cfg=cfg, series="KXBTC15M", ticker="KXBTC15M-TEST",
                  open_t=now - 4 * 60, close_t=now + 600, rule=4, side="yes", z=2.0,
                  live_enabled=False, real_cash=1000.0, real_value=1000.0, exchange_index=0, price_str=None)

    kl.process_rule_signal(cost=0.82, **common)
    n_after_entry = _total_contracts(s, "KXBTC15M-TEST", 4)

    # Same cost again -- no improvement, no add-on.
    kl.process_rule_signal(cost=0.82, **common)
    assert _total_contracts(s, "KXBTC15M-TEST", 4) == n_after_entry

    # Price moved against the position (more expensive) -- still no add-on.
    kl.process_rule_signal(cost=0.90, **common)
    assert _total_contracts(s, "KXBTC15M-TEST", 4) == n_after_entry

    log_lines = kl.LOG_PATH.read_text().splitlines()
    assert not any('"type": "addon_decision"' in l for l in log_lines)
    assert s["addon_seen"] == []


def test_addon_blocked_by_daily_loss_limit(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    common = dict(client=client, s=s, cfg=cfg, series="KXBTC15M", ticker="KXBTC15M-TEST",
                  open_t=now - 4 * 60, close_t=now + 600, rule=4, side="yes", z=2.0,
                  live_enabled=False, real_cash=1000.0, real_value=1000.0, exchange_index=0, price_str=None)

    kl.process_rule_signal(cost=0.93, **common)
    n_after_entry = _total_contracts(s, "KXBTC15M-TEST", 4)

    s["daily_realized_pnl"] = -cfg["daily_loss_limit_dollars"] - 1.0
    kl.process_rule_signal(cost=0.80, **common)
    assert _total_contracts(s, "KXBTC15M-TEST", 4) == n_after_entry, \
        "the daily loss limit must block an add-on exactly like a fresh entry"
    assert s["addon_seen"] == [], "blocked by the loss limit, not consumed -- a later cycle could still add on"
