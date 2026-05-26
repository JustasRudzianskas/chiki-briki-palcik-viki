from config import (
    CHAIN_ID,
    CLOB_HOST,
    COPY_TRADE_SIZE_USD,
    MAX_SLIPPAGE,
    PRIVATE_KEY,
    RELAYER_API_KEY,
    RELAYER_API_PASSPHRASE,
    RELAYER_API_SECRET,
)

from activity_store import save_copy_trade
from polymarket_api import (
    best_ask,
    best_bid,
    get_orderbook,
    utcnow,
)


class RealTrader:
    def __init__(self):
        self.enabled = False
        self.client = None
        self.wallet_address = None
        self._init_error = None
        self._try_init()

    def _try_init(self):
        if not PRIVATE_KEY:
            self._init_error = "PRIVATE_KEY not set"
            return
        try:
            from eth_account import Account
            from py_clob_client.client import ClobClient

            account = Account.from_key(PRIVATE_KEY)
            self.wallet_address = account.address
            self.client = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=PRIVATE_KEY,
            )
            if RELAYER_API_KEY and RELAYER_API_SECRET and RELAYER_API_PASSPHRASE:
                creds = {
                    "api_key": RELAYER_API_KEY,
                    "api_secret": RELAYER_API_SECRET,
                    "api_passphrase": RELAYER_API_PASSPHRASE,
                }
                self.client.set_api_creds(creds)
            else:
                creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(creds)
            self.enabled = True
        except Exception as e:
            self._init_error = str(e)
            self.enabled = False

    def _slippage_price(self, side: str, ref_price: float) -> float:
        if side == "BUY":
            return min(0.99, ref_price * (1 + MAX_SLIPPAGE))
        return max(0.01, ref_price * (1 - MAX_SLIPPAGE))

    def execute_real_copy(self, activity: dict) -> tuple[bool, str]:
        if not self.enabled or not self.client:
            return False, self._init_error or "Real trader not initialized"

        activity_type = str(activity.get("activity_type", "")).upper()
        if activity_type not in ("TRADE",):
            # Redeems are tracked on the UI side via paper/shadow accounting for now.
            return True, f"Real copy skipped for {activity_type}"

        token_id = activity["token_id"]
        if token_id == "unknown":
            return False, "Missing token_id"

        side = str(activity.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            return True, f"Real copy skipped for side {side}"

        book = get_orderbook(token_id)
        if not book:
            return False, "Orderbook unavailable"

        market = activity["market"]
        outcome = activity.get("outcome", "")
        aid = activity["activity_id"]
        ts = activity["timestamp"]

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            if side == "BUY":
                top = best_ask(book)
                if not top:
                    return False, "No asks in book"
                ref_price, _ = top
                target_usdc = float(activity.get("usdc_size") or 0)
                usdc_to_spend = (
                    min(COPY_TRADE_SIZE_USD, target_usdc) if target_usdc > 0 else COPY_TRADE_SIZE_USD
                )
                if usdc_to_spend <= 0:
                    return False, "Invalid BUY usdc size"
                worst = self._slippage_price("BUY", ref_price)
                mo = MarketOrderArgs(
                    token_id=token_id,
                    amount=usdc_to_spend,
                    side="BUY",
                    price=worst,
                    order_type=OrderType.FOK,
                )
                signed = self.client.create_market_order(mo)
                resp = self.client.post_order(signed, OrderType.FOK)
                size = usdc_to_spend / ref_price if ref_price else 0
                save_copy_trade(
                    mode="real",
                    source_activity_id=aid,
                    market=market,
                    token_id=token_id,
                    outcome=outcome,
                    side="BUY",
                    price=ref_price,
                    size=size,
                    usdc_size=usdc_to_spend,
                    pnl=0,
                    status="SUBMITTED",
                    order_id=str(resp.get("orderID", resp.get("id", ""))),
                    message=str(resp),
                    timestamp=ts,
                )
                return True, f"Real BUY ${usdc_to_spend} @ ~{ref_price:.4f}"

            top = best_bid(book)
            if not top:
                return False, "No bids in book"
            ref_price, _ = top
            target_usdc = float(activity.get("usdc_size") or 0)
            usdc_to_spend = (
                min(COPY_TRADE_SIZE_USD, target_usdc) if target_usdc > 0 else COPY_TRADE_SIZE_USD
            )
            if usdc_to_spend <= 0:
                return False, "Invalid SELL usdc size"
            worst = self._slippage_price("SELL", ref_price)
            shares = round(usdc_to_spend / ref_price, 4) if ref_price else 0
            if shares <= 0:
                return False, "Invalid sell share size"
            mo = MarketOrderArgs(
                token_id=token_id,
                amount=shares,
                side="SELL",
                price=worst,
                order_type=OrderType.FOK,
            )
            signed = self.client.create_market_order(mo)
            resp = self.client.post_order(signed, OrderType.FOK)
            save_copy_trade(
                mode="real",
                source_activity_id=aid,
                market=market,
                token_id=token_id,
                outcome=outcome,
                side="SELL",
                price=ref_price,
                size=shares,
                usdc_size=shares * ref_price,
                pnl=0,
                status="SUBMITTED",
                order_id=str(resp.get("orderID", resp.get("id", ""))),
                message=str(resp),
                timestamp=ts,
            )
            return True, f"Real SELL {shares} @ ~{ref_price:.4f}"

        except ImportError:
            return self._execute_limit_fallback(
                activity, book, side, token_id, market, aid, ts
            )
        except Exception as e:
            save_copy_trade(
                mode="real",
                source_activity_id=aid,
                market=market,
                token_id=token_id,
                outcome=outcome,
                side=side,
                price=0,
                size=0,
                usdc_size=0,
                pnl=0,
                status="FAILED",
                message=str(e),
                timestamp=utcnow().isoformat(),
            )
            return False, str(e)

    def _execute_limit_fallback(
        self, activity, book, side, token_id, market, aid, ts
    ) -> tuple[bool, str]:
        """Fallback if MarketOrderArgs is unavailable in installed client."""
        from py_clob_client.clob_types import OrderArgs, OrderType

        outcome = activity.get("outcome", "")
        target_usdc = float(activity.get("usdc_size") or 0)
        usdc_to_spend = (
            min(COPY_TRADE_SIZE_USD, target_usdc) if target_usdc > 0 else COPY_TRADE_SIZE_USD
        )
        if usdc_to_spend <= 0:
            return False, "Invalid fallback usdc size"

        if side == "BUY":
            top = best_ask(book)
            if not top:
                return False, "No asks"
            price, _ = top
            price = self._slippage_price("BUY", price)
            size = round(usdc_to_spend / price, 4)
            order = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
        else:
            top = best_bid(book)
            if not top:
                return False, "No bids"
            price, _ = top
            price = self._slippage_price("SELL", price)
            size = round(usdc_to_spend / price, 4)
            order = OrderArgs(token_id=token_id, price=price, size=size, side="SELL")

        signed = self.client.create_order(order)
        resp = self.client.post_order(signed, OrderType.FOK)
        save_copy_trade(
            mode="real",
            source_activity_id=aid,
            market=market,
            token_id=token_id,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
            usdc_size=usdc_to_spend,
            pnl=0,
            status="SUBMITTED",
            order_id=str(resp),
            message="Limit FOK fallback",
            timestamp=ts,
        )
        return True, f"Real {side} (limit FOK fallback)"
