"""kalshi_client: signed REST client for the Kalshi Trade API v2.

Verified directly against Kalshi's published OpenAPI spec (docs.kalshi.com/openapi.yaml)
on 2026-10-11 -- do not "fix" the signing scheme or endpoint paths from memory without
re-checking that file, since Kalshi has deprecated endpoints before (see CreateOrderV2).

Credentials are never read from code, chat, or committed files: only from the
environment variables KALSHI_KEY_ID (the API key id) and KALSHI_KEY_PATH (a file
path to the PEM-encoded private key, which stays on the user's machine). Kalshi
supports both RSA and Ed25519 keys; both are handled here (RSA-PSS/SHA-256 for
RSA, raw Ed25519 for Ed25519 -- see docs.kalshi.com's authenticated-requests page).
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
API_PREFIX = "/trade-api/v2"


class KalshiError(Exception):
    """Base class for anything that should stop the bot rather than be swallowed."""


class KalshiAuthError(KalshiError):
    pass


class KalshiAPIError(KalshiError):
    def __init__(self, status, code, message, details=""):
        self.status, self.code, self.message, self.details = status, code, message, details
        super().__init__(f"HTTP {status} {code}: {message} {details}".strip())


class KalshiNetworkError(KalshiError):
    pass


def load_private_key(key_path):
    data = Path(key_path).read_bytes()
    return serialization.load_pem_private_key(data, password=None)


def fmt_dollars(x):
    """Fixed-point dollar string Kalshi's V2 order API expects. Some markets use
    sub-penny ("tapered_deci_cent") price ticks -- confirmed live: a market quoting
    yes_ask_dollars="0.9780" and rounding that to 2 decimals ("0.98") produced
    repeated 400 Bad Request rejections on order creation. If x is already a
    string (the exact value the market quoted), pass it through unchanged rather
    than round-tripping through float and losing that precision. Only format from
    a float as a fallback, and even then keep 4 decimals, not 2.
    """
    if isinstance(x, str):
        return x
    return f"{round(float(x), 4):.4f}"


def fmt_count(n):
    return f"{int(n)}.00" if float(n).is_integer() else f"{float(n):.2f}"


class KalshiClient:
    def __init__(self, key_id=None, key_path=None, demo=False, timeout=15, max_retries=2):
        self.base = DEMO_BASE if demo else PROD_BASE
        self.demo = demo
        self.timeout = timeout
        self.max_retries = max_retries
        self.key_id = key_id
        self._private_key = load_private_key(key_path) if key_path else None
        if key_id and not self._private_key:
            raise KalshiAuthError("KALSHI_KEY_ID set but no private key loaded (KALSHI_KEY_PATH missing?)")

    @property
    def authenticated(self):
        return bool(self.key_id and self._private_key)

    def _sign(self, timestamp_ms, method, path_no_query):
        msg = f"{timestamp_ms}{method}{path_no_query}".encode("utf-8")
        if isinstance(self._private_key, ed25519.Ed25519PrivateKey):
            sig = self._private_key.sign(msg)
        elif isinstance(self._private_key, rsa.RSAPrivateKey):
            sig = self._private_key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        else:
            raise KalshiAuthError(f"unsupported private key type: {type(self._private_key)}")
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method, path):
        if not self.authenticated:
            raise KalshiAuthError("no API key/private key loaded; cannot call an authenticated endpoint")
        ts = str(int(time.time() * 1000))
        sig = self._sign(ts, method, API_PREFIX + path)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    def _request(self, method, path, params=None, body=None, auth=True):
        url = self.base + path
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
            if qs:
                url += "?" + qs
        headers = self._headers(method, path) if auth else {"User-Agent": "Mozilla/5.0"}
        if body is not None:
            body = {k: v for k, v in body.items() if v is not None}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    j = json.loads(raw)
                except Exception:
                    j = {}
                # Fall back to the raw response body (not just urllib's generic "HTTP
                # Error 400: Bad Request") when Kalshi's JSON error doesn't have a
                # "message" key -- confirmed live: a 400 on order creation logged nothing
                # but the generic urllib string, leaving no way to tell why it was
                # rejected. Surfacing the raw body means the next rejection is diagnosable.
                raw_text = raw.decode("utf-8", errors="replace").strip() if raw else ""
                message = j.get("message") or raw_text or str(e)
                raise KalshiAPIError(e.code, j.get("code", ""), message, j.get("details", ""))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise KalshiNetworkError(f"{method} {path} failed after {self.max_retries + 1} attempts: {last_err}")

    # ---- public market data (no signing) ----
    def get_markets(self, series_ticker, status="open", limit=10):
        return self._request("GET", "/markets", params=dict(series_ticker=series_ticker, status=status, limit=limit), auth=False)

    def get_market(self, ticker):
        return self._request("GET", f"/markets/{ticker}", auth=False)

    # ---- portfolio (signed) ----
    def get_balance(self, exchange_index=None):
        """Funds are SHARDED per exchange_index (confirmed live: an account can show
        ~$50 aggregate while a specific shard has under $1 actually usable there).
        Pass exchange_index to get that shard's real spendable balance instead of
        the misleading aggregate across all shards.
        """
        return self._request("GET", "/portfolio/balance", params=dict(exchange_index=exchange_index))

    def get_positions(self, settlement_status="unsettled", ticker=None, limit=200, cursor=None):
        return self._request("GET", "/portfolio/positions", params=dict(
            settlement_status=settlement_status, ticker=ticker, limit=limit, cursor=cursor))

    def get_fills(self, ticker=None, order_id=None, min_ts=None, max_ts=None, limit=200, cursor=None):
        return self._request("GET", "/portfolio/fills", params=dict(
            ticker=ticker, order_id=order_id, min_ts=min_ts, max_ts=max_ts, limit=limit, cursor=cursor))

    def get_order(self, order_id):
        """Unreliable for accounts whose orders live on a non-default exchange
        shard: this endpoint takes no exchange_index/shard hint and 404s even for
        orders that genuinely exist (confirmed live). Prefer get_orders() with a
        ticker + exchange_index filter instead; kept only for completeness.
        """
        return self._request("GET", f"/portfolio/orders/{order_id}")

    def get_orders(self, ticker=None, exchange_index=None, status=None, limit=50, cursor=None):
        return self._request("GET", "/portfolio/orders", params=dict(
            ticker=ticker, exchange_index=exchange_index, status=status, limit=limit, cursor=cursor))

    def create_order(self, ticker, book_side, count, price_dollars, client_order_id,
                      time_in_force="good_till_canceled", self_trade_prevention_type="taker_at_cross",
                      exchange_index=None):
        """book_side is 'bid' (buy YES) or 'ask' (sell YES, i.e. go long NO), priced at
        the current opposing best price so it is marketable (fills immediately against
        resting liquidity) but still a limit order -- never a market order, never
        re-priced upward to chase. Defaults to good_till_canceled: the caller is
        responsible for cancelling anything unfilled when the decision window ends
        (see kalshi_live.py's window-deadline handling).

        exchange_index: pass the market's OWN `exchange_index` field (from
        GET /markets), not -1 or omitted. Despite the spec documenting omitted/-1
        as "auto-route by ticker", that did not work in practice (confirmed live:
        consistent 404s on a healthy market until the market's actual exchange_index
        was passed explicitly) -- always read it from the market data instead.

        self_trade_prevention_type="taker_at_cross": if this new order would cross
        one of this account's own resting orders on the same market, Kalshi cancels
        the older resting (maker) order and lets this incoming (taker) order continue
        to execute against the rest of the book, rather than cancelling/shrinking the
        new order. Chosen deliberately over the alternative because our orders are
        meant to be marketable (fill now against outside liquidity) -- the bot's own
        sleeves never take opposite sides of the same ticker (side is a function of
        z-score sign, shared across rules), so a self-cross here can only come from
        an outside source (manual trading on the account, a stale resting order from
        a prior cycle); in that case we want the new signal's order to still go
        through and the stale resting order to simply be cancelled, not the reverse.
        """
        body = dict(
            ticker=ticker, side=book_side, count=fmt_count(count), price=fmt_dollars(price_dollars),
            time_in_force=time_in_force, self_trade_prevention_type=self_trade_prevention_type,
            client_order_id=client_order_id, exchange_index=exchange_index,
        )
        return self._request("POST", "/portfolio/events/orders", body=body)

    def amend_order(self, order_id, ticker, book_side, price_dollars, count, exchange_index=None):
        """Re-price (and/or re-size) a resting order in place. Used to let an order
        track the market within the decision window instead of sitting at a stale
        price -- the caller is responsible for keeping the new price within
        whatever bound still makes the bet worthwhile (e.g. signal_core.PRICE_LO/HI).
        `count` is the order's total/max fillable count (already-filled + desired
        remaining), per the spec, not just the remaining amount.
        """
        body = dict(ticker=ticker, side=book_side, price=fmt_dollars(price_dollars),
                     count=fmt_count(count), exchange_index=exchange_index)
        return self._request("POST", f"/portfolio/events/orders/{order_id}/amend", body=body)

    def cancel_order(self, order_id, market_ticker=None, exchange_index=None):
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}",
                              params=dict(market_ticker=market_ticker, exchange_index=exchange_index))

    def cancel_all_orders(self):
        """Kill-switch primitive: cancels every resting order on this account."""
        return self._request("DELETE", "/portfolio/events/orders")
