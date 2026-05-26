from config import COPY_TRADE_SIZE_USD, MAX_POSITION_PER_MARKET
from activity_store import save_copy_trade
from db import (
    get_paper_balance,
    get_paper_positions,
    set_paper_balance,
    set_paper_positions,
)


def _copy_usdc_size(target_usdc: float, target_price: float) -> tuple[float, float]:
    """Return (usdc_to_spend, shares) for a copy trade."""
    if target_usdc > 0 and target_price > 0:
        usdc = min(COPY_TRADE_SIZE_USD, target_usdc)
        shares = usdc / target_price
        return usdc, shares
    if target_price > 0:
        usdc = COPY_TRADE_SIZE_USD
        return usdc, usdc / target_price
    return COPY_TRADE_SIZE_USD, 0.0


def compute_paper_copy(
    activity: dict,
    *,
    mode: str,
    balance: float,
    positions: dict,
) -> tuple[bool, str, float, dict, dict | None, bool]:
    """
    Compute the resulting paper balance/positions for one target activity.

    Returns: (ok, message, new_balance, new_positions, copy_trade_payload, apply_state)
    where copy_trade_payload is ready for save_copy_trade(**payload).
    """
    activity_type = str(activity.get("activity_type", "")).upper()
    token_id = activity["token_id"]
    market = activity["market"]
    outcome = activity.get("outcome", "")
    price = float(activity.get("price") or 0)
    aid = activity["activity_id"]
    ts = activity["timestamp"]

    # Copy positions so compute_* never mutates the live state.
    new_positions: dict = {k: v.copy() for k, v in positions.items()}

    def payload(
        *,
        side: str,
        status: str,
        message: str,
        side_size: float = 0.0,
        side_usdc_size: float = 0.0,
        pnl: float = 0.0,
    ) -> dict:
        return {
            "mode": mode,
            "source_activity_id": aid,
            "market": market,
            "token_id": token_id,
            "outcome": outcome,
            "side": side,
            "price": price,
            "size": float(side_size),
            "usdc_size": float(side_usdc_size),
            "pnl": float(pnl),
            "status": status,
            "order_id": "",
            "message": message,
            "timestamp": ts,
        }

    if activity_type == "REDEEM":
        if token_id not in positions:
            return (
                True,
                "Redeem skipped (no position)",
                balance,
                positions,
                payload(
                    side="REDEEM",
                    status="SKIPPED",
                    message="No paper position to redeem",
                    side_size=0,
                    side_usdc_size=float(activity.get("usdc_size") or 0),
                    pnl=0,
                ),
                False,
            )

        pos = positions[token_id]
        redeem_usdc = float(activity.get("usdc_size") or pos["cost"])
        pnl = redeem_usdc - pos["cost"]
        new_balance = balance + redeem_usdc
        del new_positions[token_id]

        return (
            True,
            f"Paper redeem ${redeem_usdc:.2f}",
            new_balance,
            new_positions,
            payload(
                side="REDEEM",
                status="CLOSED",
                message="Paper redeem",
                side_size=pos["size"],
                side_usdc_size=redeem_usdc,
                pnl=pnl,
            ),
            True,
        )

    if activity_type != "TRADE":
        return True, f"Ignored type {activity_type}", balance, positions, None, False

    side = str(activity.get("side", "")).upper()
    if side not in ("BUY", "SELL"):
        return True, f"Ignored side {side}", balance, positions, None, False

    # BUY
    if side == "BUY":
        target_usdc = float(activity.get("usdc_size") or 0)
        usdc_size, shares = _copy_usdc_size(target_usdc, price)

        if shares <= 0 or usdc_size <= 0:
            return (
                False,
                "Invalid buy size",
                balance,
                positions,
                payload(
                    side="BUY",
                    status="FAILED",
                    message="Invalid buy size",
                    side_size=shares,
                    side_usdc_size=usdc_size,
                ),
                False,
            )

        if balance < usdc_size:
            return (
                False,
                "Insufficient balance",
                balance,
                positions,
                payload(
                    side="BUY",
                    status="FAILED",
                    message="Insufficient paper balance",
                    side_size=shares,
                    side_usdc_size=usdc_size,
                ),
                False,
            )

        current_cost = positions.get(token_id, {}).get("cost", 0)
        if current_cost + usdc_size > MAX_POSITION_PER_MARKET:
            return (
                False,
                "Max position exceeded",
                balance,
                positions,
                payload(
                    side="BUY",
                    status="FAILED",
                    message="Max position per market exceeded",
                    side_size=shares,
                    side_usdc_size=usdc_size,
                ),
                False,
            )

        new_balance = balance - usdc_size
        if token_id in new_positions:
            pos = new_positions[token_id]
            new_size = pos["size"] + shares
            new_cost = pos["cost"] + usdc_size
            new_entry = new_cost / new_size if new_size else price
        else:
            new_size, new_cost, new_entry = shares, usdc_size, price

        new_positions[token_id] = {
            "market": market,
            "outcome": outcome,
            "size": new_size,
            "cost": new_cost,
            "entry": new_entry,
        }

        return (
            True,
            f"Paper BUY {shares:.4f} @ {price:.4f}",
            new_balance,
            new_positions,
            payload(
                side="BUY",
                status="OPEN",
                message="Paper buy",
                side_size=shares,
                side_usdc_size=usdc_size,
                pnl=0,
            ),
            True,
        )

    # SELL
    if token_id not in positions:
        return (
            True,
            "Sell skipped (no position)",
            balance,
            positions,
            payload(
                side="SELL",
                status="SKIPPED",
                message="No paper position to sell",
                side_size=0,
                side_usdc_size=0,
                pnl=0,
            ),
            False,
        )

    pos = positions[token_id]
    target_usdc = float(activity.get("usdc_size") or 0)
    _, target_shares = _copy_usdc_size(target_usdc, price)
    sell_size = min(target_shares if target_shares > 0 else pos["size"], pos["size"])

    if sell_size <= 0:
        return (
            False,
            "Invalid sell size",
            balance,
            positions,
            payload(
                side="SELL",
                status="FAILED",
                message="Invalid sell size",
                side_size=sell_size,
                side_usdc_size=0,
                pnl=0,
            ),
            False,
        )

    usdc_size = sell_size * price if price else pos["cost"] * (sell_size / pos["size"])
    cost_basis = pos["cost"] * (sell_size / pos["size"])
    pnl = usdc_size - cost_basis

    new_balance = balance + usdc_size
    pos2 = new_positions[token_id]
    pos2["size"] -= sell_size
    pos2["cost"] -= cost_basis

    if pos2["size"] <= 1e-6:
        del new_positions[token_id]
    else:
        new_positions[token_id] = pos2

    return (
        True,
        f"Paper SELL {sell_size:.4f} PnL ${pnl:.2f}",
        new_balance,
        new_positions,
        payload(
            side="SELL",
            status="CLOSED",
            message="Paper sell",
            side_size=sell_size,
            side_usdc_size=usdc_size,
            pnl=pnl,
        ),
        True,
    )


def execute_paper_copy(activity: dict) -> tuple[bool, str]:
    """
    Copy target TRADE/REDEEM into paper portfolio.
    Returns (success, message).
    """
    balance = get_paper_balance()
    positions = get_paper_positions()

    ok, msg, new_balance, new_positions, payload, apply_state = compute_paper_copy(
        activity,
        mode="paper",
        balance=balance,
        positions=positions,
    )

    if payload is not None:
        save_copy_trade(**payload)

    if apply_state and (new_balance != balance or new_positions != positions):
        set_paper_balance(new_balance)
        set_paper_positions(new_positions)

    return ok, msg
