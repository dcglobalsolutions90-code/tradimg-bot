"""signal_core: the exact same math and decision rules as the paper bot
(reference/kalshi_multi_live_PAPER.py), with all network/account bookkeeping
stripped out. Both kalshi_live.py and tests/test_signal_parity.py import this
module so the live bot cannot drift from the paper bot's signal.

Do not "improve" anything in here. If the paper bot's math changes, change it
here and re-run the parity test.
"""
import math
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0"}

# Kalshi series -> Coinbase product id (reference/kalshi_multi_live_PAPER.py:12)
SERIES = {
    "KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
    "KXDOGE15M": "DOGE-USD", "KXXRP15M": "XRP-USD", "KXBNB15M": "BNB-USD",
    "KXHYPE15M": "HYPE-USD", "KXNEAR15M": "NEAR-USD", "KXZEC15M": "ZEC-USD",
}

# (decision minute, minimum |z|) -- the minute-3 rule (1.25 sigma) was dropped:
# -$198 over 24 bets. Do not re-add it. (spec PDF section 4)
RULES = {4: 1.5, 11: 2.0}

# The six logic sets, A to F. Each is a sleeve inside one real account.
# "stake" is the full-size bet in dollars before size_multiplier is applied.
SLEEVES = {
    "A": dict(rules=[4], stake=40.0),
    "B": dict(rules=[4], stake=100.0),
    "C": dict(rules=[11], stake=100.0),
    "D": dict(rules=[4, 11], stake=40.0),
    "E": dict(rules=[4, 11], stake=40.0),
    "F": dict(rules=[4, 11], stake=100.0),
}

PRICE_LO, PRICE_HI = 0.03, 0.97   # spec PDF section 4: 3 to 97 cents
OPEN_CAP = 0.5                    # spec PDF section 6: 50% of sleeve value in open bets


def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        import json
        return json.load(r)


def candles(prod, n=70, now=None):
    """Coinbase 1-minute candles, oldest first: [time, low, high, open, close, volume]."""
    now = time.time() if now is None else now
    end = int(now) // 60 * 60 + 60
    st = end - n * 60
    f = lambda x: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(x))
    url = f"https://api.exchange.coinbase.com/products/{prod}/candles?granularity=60&start={f(st)}&end={f(end)}"
    return sorted(http_get_json(url))


def compute_basis(rows, open_time, strike):
    """Aligns Coinbase to Kalshi's reference index at market open (spec section 3)."""
    r0 = next((r for r in rows if r[0] == int(open_time) - 60), None)
    return (strike - (r0[3] + r0[4]) / 2) if r0 else 0.0


def compute_sigma(rows):
    """Std dev of the last 60 one-minute close-to-close changes."""
    ch = [rows[i][4] - rows[i - 1][4] for i in range(max(1, len(rows) - 60), len(rows))]
    mu = sum(ch) / len(ch)
    return (sum((x - mu) ** 2 for x in ch) / len(ch)) ** 0.5


def compute_rem(close_time, now):
    return max((close_time - now) / 60 - 0.5, 0.25)


def compute_z(spot, basis, strike, sigma, rem):
    return (spot + basis - strike) / max(sigma * math.sqrt(rem), 1e-12)


def decide_side(z):
    """Side = YES if z > 0, NO if z < 0. The market odds are NEVER used here."""
    return "yes" if z > 0 else "no"


def compute_cost(side, bid, ask):
    return ask if side == "yes" else 1 - bid


def price_ok(cost, lo=PRICE_LO, hi=PRICE_HI):
    return lo <= cost <= hi


def rule_fires(minute, z, decision_minute, min_sigma=None):
    """A rule fires once minute is inside [m, m+0.5) and |z| >= its threshold."""
    min_sigma = RULES[decision_minute] if min_sigma is None else min_sigma
    return (decision_minute <= minute < decision_minute + 0.5) and abs(z) >= min_sigma


def compute_n(stake, cash, cost, fee_per_contract, open_value, equity, open_cap=OPEN_CAP):
    """Contracts to buy: the whole number that fits in the sleeve's stake, its
    open-bet cap, and available cash. Mirrors reference/kalshi_multi_live_PAPER.py:84-85
    exactly (n = int(min(stake/cost, cash/(cost+fee)))), with the cap-shrunk stake
    computed first.
    """
    stake_eff = min(stake, max(0.0, open_cap * equity - open_value))
    if cost <= 0:
        return 0
    return int(min(stake_eff // cost, cash // (cost + fee_per_contract)))
