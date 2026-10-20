"""A fake Kalshi client for the failure-mode tests. Does no network I/O; every
method's behavior is driven by the queues/flags set on the instance before the
call under test, so each test controls exactly one failure mode.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kalshi_client import KalshiAPIError, KalshiNetworkError


class FakeClient:
    def __init__(self):
        self.get_markets_error = None
        self.create_order_error = None
        self.create_order_errors = []  # optional queue, consumed one per call (None = succeed that call);
                                        # falls back to create_order_error once exhausted/unset
        self.create_order_response = None
        self.get_orders_responses = []   # list consumed one-per-call; each item is an order dict (must include order_id)
        self.cancel_order_response = {"order_id": "o1", "reduced_by": "0.00", "ts_ms": 0}
        self.positions_response = {"market_positions": []}
        self.balance_dollars = 1000.0
        self.cancel_all_called = False
        self.created_orders = []
        self.cancelled_orders = []
        self.amended_orders = []
        self.amend_order_error = None
        self.get_market_response = {"market": {"result": "", "yes_bid_dollars": "0", "yes_ask_dollars": "0"}}
        self.get_fills_response = None
        self.get_fills_error = None

    def get_markets(self, series_ticker, status="open", limit=10):
        if self.get_markets_error:
            raise self.get_markets_error
        return {"markets": []}

    def get_market(self, ticker):
        return self.get_market_response

    def get_balance(self, exchange_index=None):
        # portfolio_value is the value held in open positions, not total equity -- 0 by
        # default (no open positions), matching what the real API returns in that state.
        return {"balance_dollars": f"{self.balance_dollars:.2f}", "balance": int(self.balance_dollars * 100),
                 "portfolio_value": 0}

    def get_positions(self, **kw):
        return self.positions_response

    def get_fills(self, **kw):
        if self.get_fills_error:
            raise self.get_fills_error
        return self.get_fills_response or {"fills": []}

    def create_order(self, ticker, book_side, count, price_dollars, client_order_id, **kw):
        self.created_orders.append(dict(ticker=ticker, book_side=book_side, count=count, price=price_dollars))
        if self.create_order_errors:
            err = self.create_order_errors.pop(0)
            if err is not None:
                raise err
        elif self.create_order_error:
            raise self.create_order_error
        return self.create_order_response or {
            "order_id": "order-1", "client_order_id": client_order_id,
            "fill_count": "0.00", "remaining_count": f"{count}.00", "ts_ms": 0,
        }

    def get_order(self, order_id):
        raise AssertionError("get_order (single-id lookup) should not be used -- it 404s for sharded "
                              "accounts; the bot must use get_orders() instead")

    def get_orders(self, ticker=None, exchange_index=None, status=None, limit=50, cursor=None):
        if self.get_orders_responses:
            return {"orders": [self.get_orders_responses.pop(0)]}
        return {"orders": [{"order_id": "order-1", "status": "resting", "fill_count_fp": "0.00",
                             "taker_fill_cost_dollars": "0", "maker_fill_cost_dollars": "0",
                             "taker_fees_dollars": "0", "maker_fees_dollars": "0"}]}

    def amend_order(self, order_id, ticker, book_side, price_dollars, count, exchange_index=None):
        self.amended_orders.append(dict(order_id=order_id, ticker=ticker, book_side=book_side,
                                         price=price_dollars, count=count))
        if self.amend_order_error:
            raise self.amend_order_error
        return {"order_id": order_id, "ts_ms": 0}

    def cancel_order(self, order_id, market_ticker=None, exchange_index=None):
        self.cancelled_orders.append(order_id)
        return self.cancel_order_response

    def cancel_all_orders(self):
        self.cancel_all_called = True
        return {"ok": True}
