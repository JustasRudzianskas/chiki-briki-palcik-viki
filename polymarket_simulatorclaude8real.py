import os
import time
import requests
import pandas as pd
import streamlit as st
import plotly.express as px
from sqlalchemy import create_engine
from dotenv import load_dotenv
from datetime import datetime, timezone
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

# =========================
# CONFIG & ENVIRONMENT
# =========================
load_dotenv()

TARGET_WALLET = os.getenv("TARGET_WALLET")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", 10000))

# ============================================
# REAL TRADING SWITCH (IMPORTANT)
# ============================================
ENABLE_REAL_TRADING = os.getenv("ENABLE_REAL_TRADING", "False").lower() == "true"

# ============================================
# PRIVATE CONFIG
# ============================================
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RELAYER_API_KEY = os.getenv("RELAYER_API_KEY")
RELAYER_API_SECRET = os.getenv("RELAYER_API_SECRET")
RELAYER_API_PASSPHRASE = os.getenv("RELAYER_API_PASSPHRASE")

CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"

# ============================================
# RISK & TRADING SETTINGS
# ============================================
COPY_TRADE_SIZE_USD = 5
MAX_SLIPPAGE = 0.02
MAX_POSITION_PER_MARKET = 25
POLL_INTERVAL = 12  # seconds

DATABASE_URL = "sqlite:///trades.db"
engine = create_engine(DATABASE_URL)
session = requests.Session()

# =========================
# REAL TRADING CLIENT
# =========================
real_trading_enabled = ENABLE_REAL_TRADING and bool(PRIVATE_KEY)
clob_client = None
wallet_address = None

if real_trading_enabled:
    try:
        account = Account.from_key(PRIVATE_KEY)
        wallet_address = account.address
        
        clob_client = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=PRIVATE_KEY
        )

        if RELAYER_API_KEY and RELAYER_API_SECRET and RELAYER_API_PASSPHRASE:
            creds = {
                "api_key": RELAYER_API_KEY,
                "api_secret": RELAYER_API_SECRET,
                "api_passphrase": RELAYER_API_PASSPHRASE
            }
            clob_client.set_api_creds(creds)
        else:
            creds = clob_client.create_or_derive_api_creds()
            clob_client.set_api_creds(creds)

        st.success(f"✅ Connected to wallet: {wallet_address[:6]}...{wallet_address[-4:]}")
    except Exception as e:
        st.error(f"Failed to initialize real trading client: {e}")
        real_trading_enabled = False

# =========================
# HELPERS
# =========================
def utcnow():
    return datetime.now(timezone.utc)

# =========================
# DATABASE SETUP
# =========================
with engine.begin() as conn:
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            market TEXT,
            token_id TEXT,
            side TEXT,
            price REAL,
            size REAL,
            usdc_size REAL,
            pnl REAL,
            status TEXT
        )
    """)
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS seen_trades (
            trade_id TEXT PRIMARY KEY
        )
    """)

# =========================
# STREAMLIT SETUP
# =========================
st.set_page_config(page_title="Polymarket Copy Trader", layout="wide")
st.title("📈 Polymarket Copy Trading Bot")

if real_trading_enabled:
    st.sidebar.error("🔴 REAL TRADING ENABLED - USE WITH CAUTION")
else:
    st.sidebar.success("🟢 Paper Trading Mode")

# =========================
# SESSION STATE
# =========================
if "balance" not in st.session_state:
    st.session_state.balance = STARTING_BALANCE

if "positions" not in st.session_state:
    st.session_state.positions = {}

if "seen_trades" not in st.session_state:
    try:
        df_seen = pd.read_sql("SELECT trade_id FROM seen_trades", engine)
        st.session_state.seen_trades = set(df_seen["trade_id"].tolist())
    except:
        st.session_state.seen_trades = set()

if "last_poll" not in st.session_state:
    st.session_state.last_poll = 0

# =========================
# HELPER FUNCTIONS
# =========================
def save_trade(market, token_id, side, price, size, usdc_size, pnl, status):
    df = pd.DataFrame([{
        "timestamp": utcnow().isoformat(),
        "market": market,
        "token_id": token_id,
        "side": side,
        "price": price,
        "size": size,
        "usdc_size": usdc_size,
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

def extract_token_id(activity):
    return str(activity.get("asset") or activity.get("tokenId") or "unknown")

def extract_market_name(activity):
    return (activity.get("title") or 
            activity.get("slug") or 
            activity.get("market") or 
            "Unknown Market")

def get_orderbook(token_id):
    try:
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        r = session.get(url, timeout=8)
        return r.json() if r.status_code == 200 else None
    except:
        return None

# =========================
# PAPER TRADING
# =========================
def execute_paper_trade(activity):
    if activity.get("type") != "TRADE":
        return

    token_id = extract_token_id(activity)
    market = extract_market_name(activity)
    side = activity.get("side", "").upper()
    price = float(activity.get("price", 0))
    size = float(activity.get("size", 0))
    usdc_size = float(activity.get("usdcSize", 0) or activity.get("size", 0) * price)

    if side not in ("BUY", "SELL"):
        return

    # BUY
    if side == "BUY":
        if st.session_state.balance < usdc_size:
            return
        if token_id in st.session_state.positions:
            if st.session_state.positions[token_id]["cost"] >= MAX_POSITION_PER_MARKET:
                return

        st.session_state.balance -= usdc_size
        if token_id in st.session_state.positions:
            pos = st.session_state.positions[token_id]
            new_size = pos["size"] + size
            new_cost = pos["cost"] + usdc_size
            new_entry = new_cost / new_size
        else:
            new_size = size
            new_cost = usdc_size
            new_entry = price

        st.session_state.positions[token_id] = {
            "market": market,
            "size": new_size,
            "cost": new_cost,
            "entry": new_entry
        }
        save_trade(market, token_id, "BUY", price, size, usdc_size, 0, "OPEN")

    # SELL
    elif side == "SELL":
        if token_id not in st.session_state.positions:
            return
        pos = st.session_state.positions[token_id]
        sell_size = min(size, pos["size"])
        if sell_size <= 0:
            return

        cost_basis = pos["cost"] * (sell_size / pos["size"])
        pnl = usdc_size - cost_basis

        pos["size"] -= sell_size
        pos["cost"] -= cost_basis
        st.session_state.balance += usdc_size

        if pos["size"] <= 0.0001:
            del st.session_state.positions[token_id]

        save_trade(market, token_id, "SELL", price, sell_size, usdc_size, pnl, "CLOSED")

# =========================
# REAL TRADING
# =========================
def execute_real_trade(activity):
    if not real_trading_enabled or not clob_client:
        return
    if activity.get("type") != "TRADE":
        return

    token_id = extract_token_id(activity)
    side = activity.get("side", "").upper()

    book = get_orderbook(token_id)
    if not book:
        st.warning(f"Could not get orderbook for {token_id}")
        return

    try:
        if side == "BUY":
            asks = book.get("asks", [])
            if not asks:
                return
            price = float(asks[0]["price"])
            size = round(COPY_TRADE_SIZE_USD / price, 4)
            order = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
        else:
            bids = book.get("bids", [])
            if not bids:
                return
            price = float(bids[0]["price"])
            size = round(COPY_TRADE_SIZE_USD / price, 4)
            order = OrderArgs(token_id=token_id, price=price, size=size, side="SELL")

        signed_order = clob_client.create_order(order)
        clob_client.post_order(signed_order, OrderType.GTC)
        st.success(f"✅ REAL {side} executed: {size} @ {price}")

    except Exception as e:
        st.error(f"Real trade failed: {e}")

# =========================
# FETCH ACTIVITY
# =========================
def fetch_activity(wallet, since):
    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=100&offset=0&type=TRADE,REDEEM&startTs={since}&sortDirection=ASC"
    try:
        r = session.get(url, timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []

# =========================
# MAIN POLLING LOGIC
# =========================
st.subheader("Trading Activity")

col1, col2 = st.columns([1, 3])
with col1:
    auto_poll = st.checkbox("Auto Poll", value=True)
    manual_poll = st.button("🔄 Poll Now", type="primary")

if auto_poll or manual_poll:
    current_time = time.time()
    if manual_poll or (current_time - st.session_state.last_poll > POLL_INTERVAL):
        st.session_state.last_poll = current_time
        
        with st.spinner("Fetching latest trades..."):
            new_activity = fetch_activity(TARGET_WALLET, int(time.time()) - 3600)
            
            new_count = 0
            for item in new_activity:
                trade_id = item.get("transactionHash") or item.get("id")
                if not trade_id or trade_id in st.session_state.seen_trades:
                    continue
                
                st.session_state.seen_trades.add(trade_id)
                persist_seen_trade(trade_id)
                
                execute_paper_trade(item)
                if real_trading_enabled:
                    execute_real_trade(item)
                
                new_count += 1

            if new_count > 0:
                st.success(f"✅ Processed {new_count} new trades")
            else:
                st.info("No new trades found")

# =========================
# DASHBOARD DISPLAY
# =========================
st.divider()

col1, col2 = st.columns(2)

with col1:
    st.metric("Balance", f"${st.session_state.balance:,.2f}")

with col2:
    st.metric("Open Positions", len(st.session_state.positions))

# Positions Table
if st.session_state.positions:
    pos_data = []
    for token_id, pos in st.session_state.positions.items():
        pos_data.append({
            "Market": pos["market"],
            "Token ID": token_id[:12] + "...",
            "Size": pos["size"],
            "Entry Price": pos["entry"],
            "Cost": pos["cost"]
        })
    st.dataframe(pd.DataFrame(pos_data), use_container_width=True)
else:
    st.info("No open positions yet.")

# Recent Trades
st.subheader("Recent Trades")
try:
    df_trades = pd.read_sql("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 20", engine)
    if not df_trades.empty:
        st.dataframe(df_trades, use_container_width=True)
    else:
        st.info("No trades recorded yet.")
except:
    st.info("No trades recorded yet.")

# Auto-refresh
if auto_poll:
    time.sleep(1)  # Small delay for better UX
    st.rerun()