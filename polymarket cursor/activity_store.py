import json

import pandas as pd
from sqlalchemy import text

from db import engine
from polymarket_api import normalize_activity


def save_target_activity(item: dict) -> dict:
    n = normalize_activity(item)
    aid = n["activity_id"]
    if not aid:
        return n

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO target_activity (
                    activity_id, activity_type, timestamp, market, outcome,
                    token_id, side, price, size, usdc_size,
                    transaction_hash, raw_json
                ) VALUES (
                    :activity_id, :activity_type, :timestamp, :market, :outcome,
                    :token_id, :side, :price, :size, :usdc_size,
                    :transaction_hash, :raw_json
                )
                ON CONFLICT(activity_id) DO UPDATE SET
                    activity_type = excluded.activity_type,
                    timestamp = excluded.timestamp,
                    market = excluded.market,
                    outcome = excluded.outcome,
                    token_id = excluded.token_id,
                    side = excluded.side,
                    price = excluded.price,
                    size = excluded.size,
                    usdc_size = excluded.usdc_size,
                    transaction_hash = excluded.transaction_hash,
                    raw_json = excluded.raw_json
            """),
            {
                "activity_id": aid,
                "activity_type": n["activity_type"],
                "timestamp": n["timestamp"],
                "market": n["market"],
                "outcome": n["outcome"],
                "token_id": n["token_id"],
                "side": n["side"],
                "price": n["price"],
                "size": n["size"],
                "usdc_size": n["usdc_size"],
                "transaction_hash": n["transaction_hash"],
                "raw_json": json.dumps(item),
            },
        )
    return n


def load_target_activity(limit: int = 100) -> pd.DataFrame:
    query = f"""
        SELECT
            timestamp AS "Time (UTC)",
            activity_type AS "Type",
            market AS "Market",
            outcome AS "Outcome",
            side AS "Side",
            price AS "Price",
            size AS "Shares",
            usdc_size AS "USDC Value",
            token_id AS "Token ID",
            transaction_hash AS "Tx Hash",
            activity_id AS "Activity ID"
        FROM target_activity
        ORDER BY timestamp DESC
        LIMIT {int(limit)}
    """
    return pd.read_sql(query, engine)


def save_copy_trade(
    *,
    mode: str,
    source_activity_id: str,
    market: str,
    token_id: str,
    outcome: str,
    side: str,
    price: float,
    size: float,
    usdc_size: float,
    pnl: float,
    status: str,
    order_id: str = "",
    message: str = "",
    timestamp: str,
):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO copy_trades (
                    timestamp, mode, source_activity_id, market, token_id, outcome,
                    side, price, size, usdc_size, pnl, status, order_id, message
                ) VALUES (
                    :timestamp, :mode, :source_activity_id, :market, :token_id, :outcome,
                    :side, :price, :size, :usdc_size, :pnl, :status, :order_id, :message
                )
            """),
            {
                "timestamp": timestamp,
                "mode": mode,
                "source_activity_id": source_activity_id,
                "market": market,
                "token_id": token_id,
                "outcome": outcome,
                "side": side,
                "price": price,
                "size": size,
                "usdc_size": usdc_size,
                "pnl": pnl,
                "status": status,
                "order_id": order_id,
                "message": message,
            },
        )


def load_copy_trades(limit: int = 50) -> pd.DataFrame:
    query = f"""
        SELECT
            timestamp AS "Time (UTC)",
            mode AS "Mode",
            side AS "Side",
            market AS "Market",
            outcome AS "Outcome",
            price AS "Price",
            size AS "Shares",
            usdc_size AS "USDC",
            pnl AS "PnL",
            status AS "Status",
            order_id AS "Order ID",
            message AS "Message"
        FROM copy_trades
        ORDER BY timestamp DESC
        LIMIT {int(limit)}
    """
    return pd.read_sql(query, engine)
