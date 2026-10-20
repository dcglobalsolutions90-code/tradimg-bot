# tradimg-bot (Kalshi live bot)

> **Bring your own API key.** This repo contains no keys or account data. On first run the bot asks for *your*
> Kalshi API key ID and the path to your private key (`.pem`), and can save them to a git-ignored `.env`.
> Without a key it only runs in dry-run mode and cannot place real orders.

## Quick start

```
pip install -r requirements.txt
python setup_keys.py                # prompts for your key ID + .pem path (saved to local .env)
python kalshi_live.py               # dry run: no real orders
# real orders need BOTH the flag and the env var:
#   PowerShell:  $env:KALSHI_LIVE = "1"; python kalshi_live.py --live
```

Defaults are conservative (small stakes). `piggy.py` is an optional companion that places real orders and refuses to
start unless `KALSHI_LIVE=1`; `PIGGY_COMBO_FRAC` (e.g. `0.8`) opts into fraction-of-balance combo sizing.
`watchdog.ps1` restarts the bot if it stalls (reads credentials from `.env`).
Trading can lose all of the money in the account. Not financial advice.

---


Same signal, same six logic sets (A–F), same bet sizes as the paper bot
(`reference/kalshi_multi_live_PAPER.py`) — but this one can place real orders on
Kalshi. Read all of this before you run it with `--live`.

**This is not financial advice. Real trading can lose all of the money you put
behind this account.** The paper bot's results (310 bets, 305 wins, +$1,023.68)
are a small sample over about 36 hours, assumed perfect fills, and a single loss
at these prices (90–97 cents) costs roughly 10 to 30 wins. A backtest of 198
rules over ~10,000 past markets found one edge (minute 4, 1.5 sigma) that passed
a strict significance test only just; this has no proven edge beyond that. Start
at the default size and only raise it yourself once you've watched it work.

## Files

| File | What it is |
|---|---|
| `signal_core.py` | The exact math and rules from the paper bot (z, sigma, basis, rule thresholds, sizing). Both this bot and its tests import this file, so the signal can't quietly drift from the paper bot. |
| `kalshi_client.py` | Signed REST client for Kalshi's Trade API v2. |
| `kalshi_live.py` | The bot itself. |
| `config.json` | All limits and sleeve settings. Re-read every cycle — you can edit it while the bot runs. |
| `live_log.jsonl` | Every decision, order, fill, cancel, and settlement, one JSON object per line. |
| `live_history.jsonl` | Permanent record of every settled bet. |
| `live_state.json` | The bot's working memory between cycles (positions, orders, sleeve ledgers). Delete it to start over. |
| `tests/` | Signal parity, sizing, and simulated-failure tests. |

## 0. Install dependencies

```
pip install -r requirements.txt
```
(Just the `cryptography` package, for signing requests. Everything else is
Python standard library.)

## 1. Where your API key goes

Never give me (or paste into chat) your key or private key file. Everything
below happens only on your own machine.

1. In Kalshi's account settings, generate an API key. Kalshi will show you a
   **private key** once — save it as a `.pem` file somewhere on your machine
   (for example `C:\path\to\your\kalshi_key.pem`). It also shows you a
   **key ID** (a UUID) — keep that too.
2. Set two environment variables before you run the bot:

   PowerShell:
   ```powershell
   $env:KALSHI_KEY_ID = "your-key-id-uuid"
   $env:KALSHI_KEY_PATH = "C:\path\to\your\kalshi_key.pem"
   ```
   These only last for that PowerShell window. Don't put them in a file that
   gets committed to git — `.gitignore` already excludes `*.pem` and `.env`.
3. If you have a separate demo account/key, repeat with its own key id and
   path when you want to run `--demo`.

If these two variables aren't set (or the file doesn't exist), the bot runs in
dry run no matter what flags you pass — it cannot place real orders without
them.

## 2. Running it

**Dry run (default, safe, no key needed):**
```
python kalshi_live.py
```
Logs every decision it would have made to `live_log.jsonl` and prints a status
line every ~12 seconds. Nothing is ever sent to Kalshi in this mode. Run this
first and watch it for at least one full 15-minute market cycle.

**Demo environment (still requires your demo key to see real demo-account
behavior; add `--live` to actually place demo orders):**
```
python kalshi_live.py --demo
```

**Demo orders for real (only after you've watched the dry run and are ready):**

PowerShell:
```powershell
$env:KALSHI_LIVE = "1"
python kalshi_live.py --demo --live
```
Both `--live` and `KALSHI_LIVE=1` are required — either one alone still runs
dry. This is the test the brief asks for: place and cancel a real (demo-money)
order and see it in your Kalshi demo account.

**Real money (only after a demo run you're happy with, and only you decide
this):**
```powershell
$env:KALSHI_LIVE = "1"
python kalshi_live.py --live
```
Start with `config.json`'s default `size_multiplier` of 0.1 (a tenth of the
paper bot's sizes). Raise it yourself, by editing `config.json`, whenever
you're ready — the bot never raises it on its own.

## 3. Stopping it

- **Ctrl+C** in the terminal it's running in.
- **Create a file named `STOP`** in this folder (any contents, even empty). The
  bot checks for it every cycle.

Either way, it cancels every resting order it placed before exiting.

## 4. What the hard limits in `config.json` do

- `size_multiplier` — scales every sleeve's bet size.
- `max_total_open_pct` — never more than this fraction of your real account
  balance open across all sleeves at once (on top of each sleeve's own 50%
  cap, which mirrors the paper bot).
- `max_market_open_dollars` — never more than this many dollars open in one
  market at a time, regardless of sleeve caps.
- `max_orders_per_cycle` / `max_order_size_dollars` — caps on a single cycle's
  activity, in case several coins signal at once (the spec notes they're
  correlated and often move together).
- `daily_loss_limit_dollars` — once today's realized loss passes this, no new
  orders are placed until the next UTC day (existing orders still get
  cancelled/settled normally).
- `stale_feed_seconds` — if a coin's Coinbase feed stops advancing for this
  long, that coin is skipped; if every coin's feed is stuck like this for
  several cycles in a row, the bot halts entirely.

## 5. What's genuinely new versus the paper bot (and why it's more complex)

- Kalshi's current order API prices everything from the YES side: to bet NO you
  submit an order to *sell* YES at `1 - no_price`. The bot handles this
  translation; you never need to think about it.
- Orders rest for the 30-second decision window and are explicitly cancelled
  if unfilled when it ends — never market orders, never re-priced upward to
  chase.
- If two or more sleeves want the same contract in the same market and rule at
  the same time, the bot places **one combined order** and splits whatever
  fills (even a partial fill) across the sleeves in proportion to what each
  one asked for.
- Every cycle, the bot checks its own notion of open positions against what
  Kalshi's API actually reports. Any mismatch stops the bot and cancels open
  orders rather than guessing.
- Real fees come from Kalshi's fill data, not the paper bot's flat 2 cents.

## 6. Known limitations, honestly

- The signal-parity test (`tests/test_signal_parity.py`) replays every
  historical decision invariant (side from z, rule threshold, price limit, the
  paper bot's own fee/pnl formula) against `reference/paper_history.jsonl`. It
  cannot replay the raw Coinbase candles that produced each z, because the
  history file doesn't record them — so it validates the decision logic, not a
  byte-for-byte resimulation of raw inputs.
- Demo-environment markets mirror production but may not always have the same
  nine crypto series live at the same moments production does; if a series
  isn't open on demo, the bot just logs nothing for it, same as any other
  closed-market cycle.
- The reconciliation check compares **contract counts**, not account balance,
  because balance can move for reasons outside this bot's own bookkeeping
  (e.g. settlement credit timing). Watch the printed balance anyway.

## 7. Running the tests

```
python tests/test_signal_parity.py
python tests/test_sizing.py
python tests/test_failure_modes.py
```
All three should print `N/N passed`. These don't touch the network except
indirectly through `kalshi_live.py --once`, which is the manual dry-run test
described in section 2.
