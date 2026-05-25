import os
import time
import requests
import pandas as pd
import streamlit as st
import plotly.express as px
from sqlalchemy import create_engine
from dotenv import load_dotenv
from datetime import datetime, timezone

# =========================
# CONFIG
# =========================

load_dotenv()

TARGET_WALLET = os.getenv("TARGET_WALLET")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", 10000))

DATABASE_URL = "sqlite:///trades.db"
engine = create_engine(DATABASE_URL)

# =========================
# DATABASE SETUP
# =========================

with engine.begin() as conn:
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            market TEXT,
            token_id TEXT,
            side TEXT,
            price REAL,
            size REAL,
            pnl REAL,
            status TEXT
        )
        """
    )
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS seen_trades (
            trade_id TEXT PRIMARY KEY
        )
        """
    )

# =========================
# SESSION STATE
# =========================

if "balance" not in st.session_state:
    st.session_state.balance = STARTING_BALANCE

if "positions" not in st.session_state:
    st.session_state.positions = {}

if "equity_history" not in st.session_state:
    st.session_state.equity_history = []

if "seen_trades" not in st.session_state:
    try:
        df_seen = pd.read_sql("SELECT trade_id FROM seen_trades", engine)
        st.session_state.seen_trades = set(df_seen["trade_id"].tolist())
    except Exception:
        st.session_state.seen_trades = set()

if "initialized" not in st.session_state:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    st.session_state.tracking_since = now_ts

    try:
        offset = 0
        limit = 100
        while True:
            url = (
                f"https://data-api.polymarket.com/activity"
                f"?user={TARGET_WALLET}"
                f"&limit={limit}"
                f"&offset={offset}"
                f"&type=TRADE,REDEEM"
                f"&sortDirection=DESC"
            )
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            with engine.begin() as conn:
                for item in batch:
                    trade_id = str(item.get("transactionHash") or item.get("id", ""))
                    if trade_id:
                        st.session_state.seen_trades.add(trade_id)
                        conn.exec_driver_sql(
                            "INSERT OR IGNORE INTO seen_trades (trade_id) VALUES (?)",
                            (trade_id,)
                        )
            if len(batch) < limit:
                break
            offset += limit
    except Exception as e:
        st.warning(f"Could not pre-load history on startup: {e}")

    st.session_state.initialized = True
    st.session_state.last_checked = now_ts

elif "last_checked" not in st.session_state:
    st.session_state.last_checked = int(datetime.now(timezone.utc).timestamp())

# =========================
# API FUNCTIONS
# =========================

def fetch_new_activity(wallet, since_timestamp):
    """
    Fetch only activity after since_timestamp (unix seconds).
    ASC order ensures BUYs are always processed before their REDEEMs.
    Paginates to catch every trade in the window.
    """
    all_activity = []
    limit = 100
    offset = 0

    while True:
        url = (
            f"https://data-api.polymarket.com/activity"
            f"?user={wallet}"
            f"&limit={limit}"
            f"&offset={offset}"
            f"&type=TRADE,REDEEM"
            f"&start={since_timestamp}"
            f"&sortBy=TIMESTAMP"
            f"&sortDirection=ASC"
        )
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                st.warning(f"API returned status {response.status_code}")
                break
            batch = response.json()
        except Exception as e:
            st.error(f"API Error: {e}")
            break

        if not batch:
            break

        all_activity.extend(batch)

        if len(batch) < limit:
            break

        offset += limit

    return all_activity

# =========================
# SAVE TRADE
# =========================

def save_trade(market, token_id, side, price, size, pnl, status):
    df = pd.DataFrame([{
        "timestamp": datetime.utcnow().isoformat(),
        "market": market,
        "token_id": token_id,
        "side": side,
        "price": price,
        "size": size,
        "pnl": pnl,
        "status": status
    }])
    df.to_sql("trades", engine, if_exists="append", index=False)

def persist_seen_trade(trade_id):
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT OR IGNORE INTO seen_trades (trade_id) VALUES (?)",
            (trade_id,)
        )

# =========================
# PnL HELPER
# =========================

def get_entry_cost(token_id):
    """
    Look up the original BUY cost from DB for a given token.
    Used as fallback when the position isn't in session state
    (e.g. bot restarted between the BUY and the SELL/REDEEM).
    Returns cost in USDC or None if not found.
    """
    try:
        df = pd.read_sql(
            """
            SELECT price, size FROM trades
            WHERE token_id = ? AND side = 'BUY'
            ORDER BY id DESC LIMIT 1
            """,
            engine,
            params=(token_id,)
        )
        if not df.empty:
            return float(df.iloc[0]["price"]) * float(df.iloc[0]["size"])
    except Exception:
        pass
    return None

# =========================
# PAPER TRADING ENGINE
# =========================

def execute_paper_trade(activity):
    activity_type = activity.get("type", "TRADE")
    token_id = str(activity.get("asset", "unknown"))
    market = activity.get("title") or activity.get("slug") or "Unknown Market"

    if activity_type == "TRADE":
        side = activity.get("side", "").upper()
        if side not in ("BUY", "SELL"):
            return

        try:
            price = float(activity.get("price", 0))
            cost = float(activity.get("usdcSize", 0))
            size = float(activity.get("size", 0))
        except (ValueError, TypeError):
            return

        if cost <= 0 or size <= 0:
            return

        if side == "BUY":
            if st.session_state.balance >= cost:
                st.session_state.balance -= cost
                st.session_state.positions[token_id] = {
                    "market": market,
                    "entry": price,
                    "size": size,
                    "cost": cost,
                    "timestamp": datetime.utcnow().isoformat()
                }
                save_trade(market, token_id, "BUY", price, size, 0, "OPEN")
            else:
                st.warning(
                    f"Skipped BUY on '{market}' — insufficient balance "
                    f"(need ${cost:,.2f}, have ${st.session_state.balance:,.2f})"
                )

        elif side == "SELL":
            try:
                proceeds = float(activity.get("usdcSize", 0))
            except (ValueError, TypeError):
                proceeds = 0

            # Get cost: session state first, DB fallback, then 0
            if token_id in st.session_state.positions:
                entry_cost = st.session_state.positions[token_id].get("cost", 0)
            else:
                entry_cost = get_entry_cost(token_id) or 0

            pnl = (proceeds - entry_cost) if proceeds > 0 else 0

            st.session_state.balance += proceeds
            save_trade(market, token_id, "SELL", price, size, pnl, "CLOSED")
            st.session_state.positions.pop(token_id, None)

    elif activity_type == "REDEEM":
        try:
            size = float(activity.get("size", 0))
        except (ValueError, TypeError):
            size = 0

        redeem_price = 1.0
        payout = redeem_price * size

        # Get cost: session state first, DB fallback, then 0
        if token_id in st.session_state.positions:
            entry_cost = st.session_state.positions[token_id].get("cost", 0)
        else:
            entry_cost = get_entry_cost(token_id) or 0

        # If we still can't determine payout from size, fall back to usdcSize
        if payout <= 0:
            try:
                payout = float(activity.get("usdcSize", 0))
            except (ValueError, TypeError):
                payout = 0

        pnl = (payout - entry_cost) if payout > 0 else 0

        if payout > 0:
            st.session_state.balance += payout
            save_trade(market, token_id, "REDEEM", redeem_price, size, pnl, "REDEEMED")
            st.session_state.positions.pop(token_id, None)

# =========================
# LOAD DATA
# =========================

def load_trade_history():
    try:
        return pd.read_sql("SELECT * FROM trades ORDER BY id DESC", engine)
    except Exception:
        return pd.DataFrame()

# =========================
# DASHBOARD UI
# =========================

st.set_page_config(layout="wide")

st.title("📈 Polymarket Copy Trading Simulator")
st.markdown(f"### Tracking Wallet: `{TARGET_WALLET}`")
st.caption(
    f"Tracking live activity since: "
    f"{datetime.fromtimestamp(st.session_state.tracking_since, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
)

# =========================
# FETCH NEW ACTIVITY
# =========================

new_activity = fetch_new_activity(TARGET_WALLET, st.session_state.last_checked)

new_count = 0
for item in new_activity:
    trade_id = str(item.get("transactionHash") or item.get("id", ""))
    if not trade_id:
        continue
    if trade_id not in st.session_state.seen_trades:
        st.session_state.seen_trades.add(trade_id)
        persist_seen_trade(trade_id)
        execute_paper_trade(item)
        new_count += 1

# Advance the window so next refresh only fetches new activity
st.session_state.last_checked = int(datetime.now(timezone.utc).timestamp())

if new_count > 0:
    st.success(f"Executed {new_count} new trade(s) this refresh.")

# =========================
# METRICS
# =========================

history = load_trade_history()

realized_pnl = (
    history[history["status"].isin(["CLOSED", "REDEEMED"])]["pnl"].sum()
    if not history.empty else 0
)

open_positions_value = sum(
    pos["entry"] * pos["size"]
    for pos in st.session_state.positions.values()
)

portfolio_value = st.session_state.balance + open_positions_value

st.session_state.equity_history.append({
    "time": datetime.utcnow(),
    "equity": portfolio_value
})
st.session_state.equity_history = st.session_state.equity_history[-500:]

# =========================
# TOP METRICS
# =========================

col1, col2, col3, col4 = st.columns(4)
col1.metric("Balance", f"${st.session_state.balance:,.2f}")
col2.metric("Realized PnL", f"${realized_pnl:,.2f}")
col3.metric("Open Positions", len(st.session_state.positions))
col4.metric("Portfolio Value", f"${portfolio_value:,.2f}")

# =========================
# EQUITY CURVE
# =========================

st.subheader("Equity Curve")
curve_df = pd.DataFrame(st.session_state.equity_history)
if not curve_df.empty:
    fig = px.line(curve_df, x="time", y="equity")
    st.plotly_chart(fig, use_container_width=True)

# =========================
# OPEN POSITIONS
# =========================

st.subheader("Open Positions")
positions_list = [
    {
        "Token": token_id,
        "Market": pos["market"],
        "Entry Price": pos["entry"],
        "Size (tokens)": pos["size"],
        "Cost (USDC)": pos.get("cost", 0),
        "Opened": pos["timestamp"]
    }
    for token_id, pos in st.session_state.positions.items()
]
positions_df = pd.DataFrame(positions_list)
if positions_df.empty:
    st.info("No open positions.")
else:
    st.dataframe(positions_df, use_container_width=True)

# =========================
# CLOSED POSITIONS
# =========================

st.subheader("Closed Positions")
if not history.empty:
    closed = history[history["status"].isin(["CLOSED", "REDEEMED"])].copy()
    if closed.empty:
        st.info("No closed positions yet.")
    else:
        closed["status"] = closed["status"].map({
            "CLOSED": "✅ Sold",
            "REDEEMED": "🏆 Redeemed"
        }).fillna(closed["status"])
        display_cols = ["timestamp", "market", "token_id", "side", "price", "size", "pnl", "status"]
        st.dataframe(
            closed[display_cols].style.map(
                lambda v: "color: green" if isinstance(v, float) and v > 0
                else ("color: red" if isinstance(v, float) and v < 0 else ""),
                subset=["pnl"]
            ),
            use_container_width=True
        )
else:
    st.info("No closed positions yet.")

# =========================
# FULL TRADE HISTORY
# =========================

st.subheader("Full Trade History")
if history.empty:
    st.info("No trades yet.")
else:
    st.dataframe(history, use_container_width=True)

# =========================
# AUTO REFRESH
# =========================

st.caption("Refreshing every 10 seconds...")
time.sleep(10)
st.rerun()
