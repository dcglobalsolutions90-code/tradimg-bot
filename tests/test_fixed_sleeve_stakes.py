"""Offline regression checks for A/B fixed targets and percentage fallback."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kalshi_live as kl


def decision(monkeypatch, cash=1000.0, value=1000.0, overrides=True, state=None):
    cfg = kl.load_config()
    cfg.update(cross_spread_first_order=False, min_one_contract=False)
    if not overrides:
        cfg['sleeve_fixed_stake_dollars'] = {}
    s = state or kl.fresh_state()
    captured = {}
    monkeypatch.setattr(kl, 'log_event', lambda rec: None)

    def capture(*args, **kwargs):
        captured.update(args[10])  # requested contracts by sleeve
        return 1

    monkeypatch.setattr(kl, '_execute_order', capture)
    kl.process_rule_signal(None, s, cfg, 'KXBTC15M', 'TEST', 0, 900,
                           4, 'yes', 0.5, 2.0, False, cash, value, 0, None)
    return captured


def test_affordable_fixed_targets_and_other_sleeves_unchanged(monkeypatch):
    baseline = decision(monkeypatch, overrides=False)
    actual = decision(monkeypatch)
    assert actual['A'] == 200  # $100 at 50c
    assert actual['B'] == 100  # $50 at 50c
    for name in ('D', 'E', 'F'):
        assert actual[name] == baseline[name]


@pytest.mark.parametrize('cash, fallback_names', [(99.0, ('A',)), (49.0, ('A', 'B'))])
def test_low_cash_uses_original_percentage_allocation(monkeypatch, cash, fallback_names):
    baseline = decision(monkeypatch, cash=cash, overrides=False)
    actual = decision(monkeypatch, cash=cash)
    for name in fallback_names:
        assert actual[name] == baseline[name]


def test_fee_reserve_required_before_fixed_target(monkeypatch):
    # $100 buys 200 contracts at 50c, requiring $104 including fee estimate.
    baseline = decision(monkeypatch, cash=100.0, overrides=False)
    actual = decision(monkeypatch, cash=100.0)
    assert actual['A'] == baseline['A']
    assert actual['B'] == 100


def test_fixed_targets_return_when_cash_recovers(monkeypatch):
    assert decision(monkeypatch, cash=49.0)['A'] < 200
    assert decision(monkeypatch, cash=1000.0)['A'] == 200


def test_account_limit_reduces_combined_targets(monkeypatch):
    actual = decision(monkeypatch, cash=200.0, value=200.0)
    assert sum(actual.values()) * 0.5 <= 80.0


def test_one_contract_minimum_cannot_bypass_exhausted_account_cap(monkeypatch):
    s = kl.fresh_state()
    s['positions']['OLD'] = dict(ticker='OTHER', rule=4, side='yes', close=900,
                                sleeve_contracts={'F': 800}, sleeve_cost={'F': 400.0})
    cfg = kl.load_config()
    cfg['min_one_contract'] = True
    monkeypatch.setattr(kl, 'load_config', lambda: cfg)
    monkeypatch.setattr(kl, 'log_event', lambda rec: None)
    monkeypatch.setattr(kl, '_execute_order', lambda *a, **k: pytest.fail('Exposure cap bypassed'))
    assert kl.process_rule_signal(None, s, cfg, 'KXBTC15M', 'TEST', 0, 900,
                                  4, 'yes', 0.5, 2.0, False, 1000.0, 1000.0, 0, None) == 0
