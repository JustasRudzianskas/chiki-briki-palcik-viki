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

session = requests.Session()

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
# STREAMLIT PAGE
# =========================

st.set_page_config(layout="wide")

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

        df_seen = pd.read_sql(
            "SELECT trade_id FROM seen_trades",
            engine
        )

        st.session_state.seen_trades = set(
            df_seen["trade_id"].tolist()
        )

    except Exception:

        st.session_state.seen_trades = set()

if "initialized" not in st.session_state:

    now_ts = int(datetime.now(timezone.utc).timestamp())

    st.session_state.tracking_since = now_ts
    st.session_state.last_checked = now_ts

    # =========================
    # PRELOAD OLD TRADES
    # =========================

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

            response = session.get(url, timeout=10)

            if response.status_code != 200:
                break

            batch = response.json()

            if not batch:
                break

            with engine.begin() as conn:

                for item in batch:

                    trade_id = str(
                        item.get("transactionHash")
                        or item.get("id", "")
                    )

                    if trade_id:

                        st.session_state.seen_trades.add(trade_id)

                        conn.exec_driver_sql(
                            """
                            INSERT OR IGNORE INTO seen_trades (trade_id)
                            VALUES (?)
                            """,
                            (trade_id,)
                        )

            if len(batch) < limit:
                break

            offset += limit

    except Exception as e:

        st.warning(
            f"Could not preload trade history: {e}"
        )

    st.session_state.initialized = True

# =========================
# DATABASE HELPERS
# =========================

def save_trade(
    market,
    token_id,
    side,
    price,
    size,
    pnl,
    status
):

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

    df.to_sql(
        "trades",
        engine,
        if_exists="append",
        index=False
    )


def persist_seen_trade(trade_id):

    with engine.begin() as conn:

        conn.exec_driver_sql(
            """
            INSERT OR IGNORE INTO seen_trades (trade_id)
            VALUES (?)
            """,
            (trade_id,)
        )

# =========================
# POSITION RECONSTRUCTION
# =========================

def get_position_from_db(token_id):

    try:

        df = pd.read_sql(
            """
            SELECT *
            FROM trades
            WHERE token_id = ?
            ORDER BY id ASC
            """,
            engine,
            params=(token_id,)
        )

        if df.empty:
            return None

        size = 0
        cost = 0
        market = "Unknown"

        for _, row in df.iterrows():

            market = row["market"]

            side = row["side"]

            trade_size = float(row["size"])
            trade_price = float(row["price"])

            # =========================
            # BUY
            # =========================

            if side == "BUY":

                size += trade_size
                cost += trade_size * trade_price

            # =========================
            # SELL / REDEEM
            # =========================

            elif side in ("SELL", "REDEEM"):

                if size <= 0:
                    continue

                reduction_ratio = min(
                    trade_size / size,
                    1
                )

                cost_reduction = cost * reduction_ratio

                size -= trade_size
                cost -= cost_reduction

                size = max(size, 0)
                cost = max(cost, 0)

        if size <= 0:
            return None

        return {
            "market": market,
            "size": size,
            "cost": cost,
            "entry": cost / size,
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception:
        return None

# =========================
# REBUILD OPEN POSITIONS
# =========================

if not st.session_state.positions:

    try:

        history = pd.read_sql(
            """
            SELECT DISTINCT token_id
            FROM trades
            """,
            engine
        )

        for token_id in history["token_id"]:

            reconstructed = get_position_from_db(token_id)

            if reconstructed:

                st.session_state.positions[token_id] = reconstructed

    except Exception as e:

        st.warning(
            f"Position reconstruction failed: {e}"
        )

# =========================
# API FUNCTIONS
# =========================

def fetch_new_activity(
    wallet,
    since_timestamp
):

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

            response = session.get(
                url,
                timeout=10
            )

            if response.status_code != 200:

                st.warning(
                    f"API status: {response.status_code}"
                )

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
# PAPER TRADING ENGINE
# =========================

def execute_paper_trade(activity):

    activity_type = activity.get(
        "type",
        "TRADE"
    )

    token_id = str(
        activity.get("asset", "unknown")
    )

    market = (
        activity.get("title")
        or activity.get("slug")
        or "Unknown Market"
    )

    # =========================
    # TRADE
    # =========================

    if activity_type == "TRADE":

        side = activity.get(
            "side",
            ""
        ).upper()

        if side not in ("BUY", "SELL"):
            return

        try:

            price = float(
                activity.get("price", 0)
            )

            size = float(
                activity.get("size", 0)
            )

            usdc_size = float(
                activity.get("usdcSize", 0)
            )

        except (ValueError, TypeError):

            return

        if size <= 0 or usdc_size <= 0:
            return

        # =========================
        # BUY
        # =========================

        if side == "BUY":

            if st.session_state.balance < usdc_size:

                st.warning(
                    f"Skipped BUY '{market}' "
                    f"(need ${usdc_size:.2f}, "
                    f"have ${st.session_state.balance:.2f})"
                )

                return

            st.session_state.balance -= usdc_size

            # Existing position
            if token_id in st.session_state.positions:

                pos = st.session_state.positions[token_id]

                new_size = pos["size"] + size
                new_cost = pos["cost"] + usdc_size

                avg_entry = new_cost / new_size

                st.session_state.positions[token_id] = {
                    "market": market,
                    "size": new_size,
                    "cost": new_cost,
                    "entry": avg_entry,
                    "timestamp": pos["timestamp"]
                }

            # New position
            else:

                st.session_state.positions[token_id] = {
                    "market": market,
                    "size": size,
                    "cost": usdc_size,
                    "entry": price,
                    "timestamp": datetime.utcnow().isoformat()
                }

            save_trade(
                market,
                token_id,
                "BUY",
                price,
                size,
                0,
                "OPEN"
            )

        # =========================
        # SELL
        # =========================

        elif side == "SELL":

            if token_id in st.session_state.positions:

                pos = st.session_state.positions[token_id]

            else:

                reconstructed = get_position_from_db(
                    token_id
                )

                if not reconstructed:
                    return

                pos = reconstructed

            current_size = pos["size"]
            current_cost = pos["cost"]

            sell_ratio = min(
                size / current_size,
                1
            )

            cost_basis_closed = (
                current_cost * sell_ratio
            )

            pnl = usdc_size - cost_basis_closed

            remaining_size = (
                current_size - size
            )

            remaining_cost = (
                current_cost - cost_basis_closed
            )

            st.session_state.balance += usdc_size

            save_trade(
                market,
                token_id,
                "SELL",
                price,
                size,
                pnl,
                "CLOSED"
            )

            # Partial close
            if remaining_size > 0:

                st.session_state.positions[token_id] = {
                    "market": market,
                    "size": remaining_size,
                    "cost": remaining_cost,
                    "entry": (
                        remaining_cost / remaining_size
                    ),
                    "timestamp": pos["timestamp"]
                }

            # Fully closed
            else:

                st.session_state.positions.pop(
                    token_id,
                    None
                )

    # =========================
    # REDEEM
    # =========================

    elif activity_type == "REDEEM":

        try:

            size = float(
                activity.get("size", 0)
            )

        except (ValueError, TypeError):

            return

        if size <= 0:
            return

        payout = size * 1.0

        if token_id in st.session_state.positions:

            pos = st.session_state.positions[token_id]

        else:

            reconstructed = get_position_from_db(
                token_id
            )

            if not reconstructed:
                return

            pos = reconstructed

        current_size = pos["size"]
        current_cost = pos["cost"]

        redeem_ratio = min(
            size / current_size,
            1
        )

        cost_basis_closed = (
            current_cost * redeem_ratio
        )

        pnl = payout - cost_basis_closed

        remaining_size = (
            current_size - size
        )

        remaining_cost = (
            current_cost - cost_basis_closed
        )

        st.session_state.balance += payout

        save_trade(
            market,
            token_id,
            "REDEEM",
            1.0,
            size,
            pnl,
            "REDEEMED"
        )

        # Partial redeem
        if remaining_size > 0:

            st.session_state.positions[token_id] = {
                "market": market,
                "size": remaining_size,
                "cost": remaining_cost,
                "entry": (
                    remaining_cost / remaining_size
                ),
                "timestamp": pos["timestamp"]
            }

        # Fully redeemed
        else:

            st.session_state.positions.pop(
                token_id,
                None
            )

# =========================
# LOAD TRADE HISTORY
# =========================

def load_trade_history():

    try:

        return pd.read_sql(
            """
            SELECT *
            FROM trades
            ORDER BY id DESC
            """,
            engine
        )

    except Exception:

        return pd.DataFrame()

# =========================
# DASHBOARD HEADER
# =========================

st.title(
    "📈 Polymarket Copy Trading Simulator"
)

st.markdown(
    f"### Tracking Wallet: `{TARGET_WALLET}`"
)

st.caption(
    f"Tracking live activity since: "
    f"{datetime.fromtimestamp(st.session_state.tracking_since, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
)

# =========================
# FETCH NEW ACTIVITY
# =========================

new_activity = fetch_new_activity(
    TARGET_WALLET,
    st.session_state.last_checked
)

new_count = 0

for item in new_activity:

    trade_id = str(
        item.get("transactionHash")
        or item.get("id", "")
    )

    if not trade_id:
        continue

    if trade_id not in st.session_state.seen_trades:

        st.session_state.seen_trades.add(
            trade_id
        )

        persist_seen_trade(trade_id)

        execute_paper_trade(item)

        new_count += 1

# =========================
# ADVANCE TIMESTAMP WINDOW
# =========================

if new_activity:

    newest_ts = max(
        int(
            item.get(
                "timestamp",
                st.session_state.last_checked
            )
        )
        for item in new_activity
    )

    st.session_state.last_checked = (
        newest_ts - 1
    )

else:

    st.session_state.last_checked = int(
        datetime.now(timezone.utc).timestamp()
    )

# =========================
# STATUS
# =========================

if new_count > 0:

    st.success(
        f"Executed {new_count} "
        f"new trade(s)."
    )

# =========================
# METRICS
# =========================

history = load_trade_history()

realized_pnl = (
    history[
        history["status"].isin(
            ["CLOSED", "REDEEMED"]
        )
    ]["pnl"].sum()
    if not history.empty
    else 0
)

open_positions_value = sum(
    pos["cost"]
    for pos in st.session_state.positions.values()
)

portfolio_value = (
    st.session_state.balance
    + open_positions_value
)

st.session_state.equity_history.append({
    "time": datetime.utcnow(),
    "equity": portfolio_value
})

st.session_state.equity_history = (
    st.session_state.equity_history[-500:]
)

# =========================
# TOP METRICS
# =========================

col1, col2, col3, col4 = st.columns(4)

col1.metric(
    "Balance",
    f"${st.session_state.balance:,.2f}"
)

col2.metric(
    "Realized PnL",
    f"${realized_pnl:,.2f}"
)

col3.metric(
    "Open Positions",
    len(st.session_state.positions)
)

col4.metric(
    "Portfolio Value",
    f"${portfolio_value:,.2f}"
)

# =========================
# EQUITY CURVE
# =========================

st.subheader("Equity Curve")

curve_df = pd.DataFrame(
    st.session_state.equity_history
)

if not curve_df.empty:

    fig = px.line(
        curve_df,
        x="time",
        y="equity"
    )

    st.plotly_chart(
        fig,
        use_container_width=True
    )

# =========================
# OPEN POSITIONS
# =========================

st.subheader("Open Positions")

positions_list = []

for token_id, pos in st.session_state.positions.items():

    positions_list.append({
        "Token": token_id,
        "Market": pos["market"],
        "Entry Price": round(pos["entry"], 4),
        "Size": round(pos["size"], 4),
        "Cost Basis": round(pos["cost"], 2),
        "Opened": pos["timestamp"]
    })

positions_df = pd.DataFrame(
    positions_list
)

if positions_df.empty:

    st.info("No open positions.")

else:

    st.dataframe(
        positions_df,
        use_container_width=True
    )

# =========================
# CLOSED POSITIONS
# =========================

st.subheader("Closed Positions")

if not history.empty:

    closed = history[
        history["status"].isin(
            ["CLOSED", "REDEEMED"]
        )
    ].copy()

    if closed.empty:

        st.info(
            "No closed positions yet."
        )

    else:

        closed["status"] = closed[
            "status"
        ].map({
            "CLOSED": "✅ Sold",
            "REDEEMED": "🏆 Redeemed"
        }).fillna(closed["status"])

        display_cols = [
            "timestamp",
            "market",
            "token_id",
            "side",
            "price",
            "size",
            "pnl",
            "status"
        ]

        st.dataframe(
            closed[display_cols].style.map(
                lambda v:
                    "color: green"
                    if isinstance(v, float) and v > 0
                    else (
                        "color: red"
                        if isinstance(v, float) and v < 0
                        else ""
                    ),
                subset=["pnl"]
            ),
            use_container_width=True
        )

else:

    st.info(
        "No closed positions yet."
    )

# =========================
# FULL TRADE HISTORY
# =========================

st.subheader("Full Trade History")

if history.empty:

    st.info("No trades yet.")

else:

    st.dataframe(
        history,
        use_container_width=True
    )

# =========================
# AUTO REFRESH
# =========================

st.caption(
    "Refreshing every 10 seconds..."
)

time.sleep(10)

st.rerun()