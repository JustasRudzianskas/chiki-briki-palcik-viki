import os
import time
import requests
import pandas as pd
import streamlit as st
import plotly.express as px
from sqlalchemy import create_engine
from dotenv import load_dotenv
from datetime import datetime

# =========================
# CONFIG
# =========================

load_dotenv()

TARGET_WALLET = os.getenv("TARGET_WALLET")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", 10000))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", 0.02))

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

# =========================
# API FUNCTIONS
# =========================

def fetch_wallet_activity(wallet):
    """
    Fetch TRADE and REDEEM activity from Polymarket with pagination.

    IMPORTANT:
    - type= uses comma-separated format (style: form, explode: false)
      so ?type=TRADE,REDEEM is correct — NOT ?type=TRADE&type=REDEEM
    - sortDirection=ASC so oldest events come first — this ensures BUYs
      are always processed before the REDEEMs that close them
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
            f"&sortBy=TIMESTAMP"
            f"&sortDirection=ASC"   # oldest first — BUYs processed before REDEEMs
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

        if len(all_activity) >= 500:
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
            price = float(activity.get("price", 0.5))
        except (ValueError, TypeError):
            price = 0.5

        try:
            cost = float(activity.get("usdcSize", 0))  # exact USDC they spent
            size = float(activity.get("size", 0))       # exact tokens they got
        except (ValueError, TypeError):
            return  # if we can't read the size, skip rather than guess

        if side == "BUY":
            if st.session_state.balance >= cost:
                st.session_state.balance -= cost
                st.session_state.positions[token_id] = {
                    "market": market,
                    "entry": price,
                    "size": size,
                    "timestamp": datetime.utcnow().isoformat()
                }
                save_trade(market, token_id, "BUY", price, size, 0, "OPEN")
            else:
                st.warning(
                    f"Skipped BUY on '{market}' — insufficient balance "
                    f"(need ${cost:,.2f}, have ${st.session_state.balance:,.2f})"
                )

        elif side == "SELL":
            if token_id in st.session_state.positions:
                pos = st.session_state.positions[token_id]
                pnl = (price - pos["entry"]) * pos["size"]
                st.session_state.balance += price * pos["size"]
                save_trade(market, token_id, "SELL", price, pos["size"], pnl, "CLOSED")
                del st.session_state.positions[token_id]

    elif activity_type == "REDEEM":
        # Market resolved — winning tokens pay out at $1.00 each.
        # Because we fetch ASC (oldest first), the BUY is always processed
        # before this redeem, so the position should exist in session state.
        if token_id in st.session_state.positions:
            pos = st.session_state.positions[token_id]
            redeem_price = 1.0
            pnl = (redeem_price - pos["entry"]) * pos["size"]
            payout = redeem_price * pos["size"]
            st.session_state.balance += payout
            save_trade(market, token_id, "REDEEM", redeem_price, pos["size"], pnl, "REDEEMED")
            del st.session_state.positions[token_id]
        else:
            # Position not tracked (bot started mid-stream after the BUY).
            # Fall back to usdcSize from API as the payout.
            try:
                usdc = float(activity.get("usdcSize", 0))
                size = float(activity.get("size", 0))
            except (ValueError, TypeError):
                usdc = 0
                size = 0
            if usdc > 0:
                st.session_state.balance += usdc
                save_trade(market, token_id, "REDEEM", 1.0, size, usdc, "REDEEMED")

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

# =========================
# FETCH NEW ACTIVITY
# =========================

activity = fetch_wallet_activity(TARGET_WALLET)

for item in activity:
    trade_id = str(item.get("transactionHash") or item.get("id", ""))
    if not trade_id:
        continue
    if trade_id not in st.session_state.seen_trades:
        st.session_state.seen_trades.add(trade_id)
        persist_seen_trade(trade_id)
        execute_paper_trade(item)

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
        "Entry": pos["entry"],
        "Size": pos["size"],
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
