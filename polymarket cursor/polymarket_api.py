import time
from datetime import datetime, timezone

import requests

from config import CLOB_HOST, DATA_API_HOST, LOOKBACK_HOURS

session = requests.Session()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def activity_id(item: dict) -> str | None:
    return (
        item.get("transactionHash")
        or item.get("id")
        or item.get("tradeId")
    )


def activity_timestamp(item: dict) -> str:
    """
    Polymarket activity uses `timestamp` as Unix seconds (see Data API docs).
    Always prefer that field — other fields can be market metadata with wrong dates.
    """
    val = item.get("timestamp")
    if val is None:
        return utcnow().isoformat()

    if isinstance(val, (int, float)):
        ts = float(val)
        # Values > 1e12 are milliseconds; API docs use seconds.
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            ts = float(s)
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        # ISO-8601 string from API
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            return s

    return utcnow().isoformat()


def extract_token_id(activity: dict) -> str:
    return str(
        activity.get("asset")
        or activity.get("tokenId")
        or activity.get("token_id")
        or "unknown"
    )


def extract_market_name(activity: dict) -> str:
    return (
        activity.get("title")
        or activity.get("question")
        or activity.get("slug")
        or activity.get("market")
        or activity.get("eventSlug")
        or "Unknown Market"
    )


def extract_outcome(activity: dict) -> str:
    return (
        activity.get("outcome")
        or activity.get("outcomeName")
        or activity.get("outcomeIndex")
        or ""
    )


def normalize_activity(item: dict) -> dict:
    price = float(item.get("price") or 0)
    size = float(item.get("size") or item.get("shares") or 0)
    usdc = float(
        item.get("usdcSize")
        or item.get("usdc_size")
        or item.get("amount")
        or (size * price if price and size else 0)
    )
    return {
        "activity_id": activity_id(item),
        "activity_type": str(item.get("type", "UNKNOWN")).upper(),
        "timestamp": activity_timestamp(item),
        "market": extract_market_name(item),
        "outcome": str(extract_outcome(item)),
        "token_id": extract_token_id(item),
        "side": str(item.get("side", "")).upper(),
        "price": price,
        "size": size,
        "usdc_size": usdc,
        "transaction_hash": item.get("transactionHash") or "",
        "raw": item,
    }


def fetch_activity(wallet: str, since_ts: int | None = None) -> list[dict]:
    if not wallet:
        return []
    if since_ts is None:
        since_ts = int(time.time()) - LOOKBACK_HOURS * 3600

    # Official param is `start` (seconds), not `startTs`. Wrong param = old trades mixed in.
    url = (
        f"{DATA_API_HOST}/activity"
        f"?user={wallet}"
        f"&limit=200"
        f"&offset=0"
        f"&type=TRADE,REDEEM"
        f"&start={since_ts}"
        f"&sortBy=TIMESTAMP"
        f"&sortDirection=DESC"
    )
    try:
        r = session.get(url, timeout=12)
        if r.status_code != 200:
            return []
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("data", data.get("activities", []))
    except requests.RequestException:
        return []


def get_orderbook(token_id: str) -> dict | None:
    try:
        url = f"{CLOB_HOST}/book?token_id={token_id}"
        r = session.get(url, timeout=8)
        return r.json() if r.status_code == 200 else None
    except requests.RequestException:
        return None


def best_ask(book: dict) -> tuple[float, float] | None:
    asks = book.get("asks") or []
    if not asks:
        return None
    row = asks[0]
    return float(row["price"]), float(row.get("size", 0))


def best_bid(book: dict) -> tuple[float, float] | None:
    bids = book.get("bids") or []
    if not bids:
        return None
    row = bids[0]
    return float(row["price"]), float(row.get("size", 0))
