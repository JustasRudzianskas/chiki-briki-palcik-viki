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
    # Load persisted seen_trades from DB so restarts don't re-execute old trades
    try:
        df_seen = pd.read_sql("SELECT trade_id FROM seen_trades", engine)
        st.session_state.seen_trades = set(df_seen["trade_id"].tolist())
    except Exception:
        st.session_state.seen_trades = set()

# =========================
# API FUNCTIONS
# =========================

def fetch_wallet_activity(wallet):
    """Fetch wallet activity from Polymarket."""
    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=50"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        st.error(f"API Error: {e}")
    return []

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
    """Save a seen trade ID to DB so it survives restarts."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT OR IGNORE INTO seen_trades (trade_id) VALUES (?)",
            (trade_id,)
        )

# =========================
# PAPER TRADING ENGINE
# =========================

def execute_paper_trade(trade):
    token_id = str(trade.get("asset", "unknown"))
    market = trade.get("market_slug", "Unknown Market")
    side = trade.get("side", "BUY")

    try:
        price = float(trade.get("price", 0.5))
    except (ValueError, TypeError):
        price = 0.5

    risk_amount = st.session_state.balance * RISK_PER_TRADE
    size = max(1, risk_amount / max(price, 0.01))
    cost = price * size

    if side.upper() == "BUY":
        if st.session_state.balance >= cost:
            st.session_state.balance -= cost
            st.session_state.positions[token_id] = {
                "market": market,
                "entry": price,
                "size": size,
                "timestamp": datetime.utcnow().isoformat()
            }
            save_trade(market, token_id, side, price, size, 0, "OPEN")

    elif side.upper() == "SELL":
        if token_id in st.session_state.positions:
            pos = st.session_state.positions[token_id]
            pnl = (price - pos["entry"]) * pos["size"]

            # FIX: only add sale proceeds — pnl is already included in that
            # (sale proceeds = entry * size + profit, which equals price * size)
            st.session_state.balance += price * pos["size"]

            save_trade(market, token_id, side, price, pos["size"], pnl, "CLOSED")
            del st.session_state.positions[token_id]

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

# FIX: loop is self-contained — metrics/UI are outside it
for trade in activity:
    trade_id = str(trade.get("id"))
    if trade_id not in st.session_state.seen_trades:
        st.session_state.seen_trades.add(trade_id)
        persist_seen_trade(trade_id)
        execute_paper_trade(trade)

# =========================
# METRICS
# =========================

history = load_trade_history()

realized_pnl = history["pnl"].sum() if not history.empty else 0

open_positions_value = sum(
    pos["entry"] * pos["size"]
    for pos in st.session_state.positions.values()
)

# FIX: realized_pnl is already baked into balance via SELL logic, don't double-add
portfolio_value = st.session_state.balance + open_positions_value

st.session_state.equity_history.append({
    "time": datetime.utcnow(),
    "equity": portfolio_value
})

# Cap equity history to last 500 points to avoid memory bloat
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
# TRADE HISTORY
# =========================

st.subheader("Trade History")

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