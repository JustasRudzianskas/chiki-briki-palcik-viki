import time

import pandas as pd
import streamlit as st

from activity_store import (
    load_copy_trades,
    load_target_activity,
    save_copy_trade,
    save_target_activity,
)
from config import (
    COPY_TRADE_SIZE_USD,
    ENABLE_REAL_TRADING,
    LOOKBACK_HOURS,
    MAX_POSITION_PER_MARKET,
    MAX_SLIPPAGE,
    POLL_INTERVAL,
    STARTING_BALANCE,
    TARGET_WALLET,
)
from db import (
    get_paper_balance,
    get_paper_positions,
    get_processed_ids,
    init_db,
    mark_processed,
    set_paper_balance,
    set_paper_positions,
)
from paper_trader import compute_paper_copy, execute_paper_copy
from polymarket_api import fetch_activity, utcnow
from real_trader import RealTrader

init_db()

st.set_page_config(page_title="Polymarket Copy Trader", layout="wide")
st.title("Polymarket Copy Trading Bot")

# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------
st.sidebar.header("Controls")

if not TARGET_WALLET:
    st.error("Set `TARGET_WALLET` in your `.env` file.")
    st.stop()

trading_mode = st.sidebar.radio(
    "Execution mode",
    options=["paper", "real"],
    format_func=lambda x: "Paper trading" if x == "paper" else "Real trading",
    index=0 if not ENABLE_REAL_TRADING else 1,
    help="Paper simulates copies locally. Real sends orders via CLOB.",
)

real_trader = RealTrader()
real_active = trading_mode == "real"

if real_active:
    if real_trader.enabled:
        addr = real_trader.wallet_address or ""
        st.sidebar.error(
            f"REAL TRADING ON — {addr[:6]}...{addr[-4:]}" if addr else "REAL TRADING ON"
        )
    else:
        st.sidebar.warning(
            f"Real trading selected but client failed: {real_trader._init_error}"
        )
        real_active = False
else:
    st.sidebar.success("Paper trading mode")

auto_poll = st.sidebar.checkbox("Auto refresh", value=False)
poll_now = st.sidebar.button("Poll now", type="primary")

st.sidebar.divider()
st.sidebar.caption("Settings")
st.sidebar.write(f"Target: `{TARGET_WALLET[:8]}...{TARGET_WALLET[-4:]}`")
st.sidebar.write(f"Copy size: ${COPY_TRADE_SIZE_USD}")
st.sidebar.write(f"Max slippage: {MAX_SLIPPAGE * 100:.1f}%")
st.sidebar.write(f"Max / market: ${MAX_POSITION_PER_MARKET}")
st.sidebar.write(f"Lookback: {LOOKBACK_HOURS}h | Poll: {POLL_INTERVAL}s")

if st.sidebar.button("Reset paper balance"):
    set_paper_balance(STARTING_BALANCE)
    set_paper_positions({})
    st.session_state.pop("last_poll_msg", None)
    st.rerun()

# ---------------------------------------------------------------------------
# Poll target wallet
# ---------------------------------------------------------------------------
def run_poll() -> dict:
    since_ts = int(time.time()) - LOOKBACK_HOURS * 3600
    items = fetch_activity(TARGET_WALLET, since_ts=since_ts)
    processed = get_processed_ids()

    new_activity = 0
    copied = 0
    skipped = 0
    errors: list[str] = []

    for raw in items:
        normalized = save_target_activity(raw)
        aid = normalized.get("activity_id")
        if not aid:
            continue
        if aid in processed:
            continue

        new_activity += 1
        activity_type = str(normalized.get("activity_type") or "").upper()

        if trading_mode == "paper":
            ok, msg = execute_paper_copy(normalized)
            if ok:
                mark_processed(aid, utcnow().isoformat())
                processed.add(aid)
                if "skipped" not in msg.lower() and "ignored" not in msg.lower():
                    copied += 1
            else:
                errors.append(f"{aid[:10]}...: {msg}")
            continue

        if not real_active:
            skipped += 1
            continue

        # Real mode: validate/calculate shadow sizing first.
        balance = get_paper_balance()
        positions = get_paper_positions()
        ok_shadow, msg_shadow, new_balance, new_positions, payload, apply_state = (
            compute_paper_copy(
                normalized,
                mode="real",
                balance=balance,
                positions=positions,
            )
        )

        # Unknown/unsupported types (payload None) are simply marked processed.
        if payload is None:
            mark_processed(aid, utcnow().isoformat())
            processed.add(aid)
            skipped += 1
            continue

        # Deterministic strategy failures (e.g. no paper balance / max per market).
        if not ok_shadow:
            save_copy_trade(**payload)
            mark_processed(aid, utcnow().isoformat())
            processed.add(aid)
            skipped += 1
            continue

        # Redeems are tracked by shadow accounting only in this script.
        if activity_type == "REDEEM":
            save_copy_trade(**payload)
            if apply_state:
                set_paper_balance(new_balance)
                set_paper_positions(new_positions)
            mark_processed(aid, utcnow().isoformat())
            processed.add(aid)
            copied += 1
            continue

        # For skipped TRADEs (e.g. SELL with no position), just log and mark processed.
        if payload.get("status") == "SKIPPED":
            save_copy_trade(**payload)
            mark_processed(aid, utcnow().isoformat())
            processed.add(aid)
            skipped += 1
            continue

        # TRADE: place real order, then apply shadow balance/positions.
        ok, msg = real_trader.execute_real_copy(normalized)
        if ok:
            if apply_state:
                set_paper_balance(new_balance)
                set_paper_positions(new_positions)
            mark_processed(aid, utcnow().isoformat())
            processed.add(aid)
            copied += 1
        else:
            errors.append(f"{aid[:10]}...: {msg}")

    return {
        "fetched": len(items),
        "new_activity": new_activity,
        "copied": copied,
        "skipped": skipped,
        "errors": errors,
    }


should_poll = poll_now
if auto_poll:
    if "last_poll_ts" not in st.session_state:
        st.session_state.last_poll_ts = 0
    if time.time() - st.session_state.last_poll_ts >= POLL_INTERVAL:
        should_poll = True

if should_poll:
    with st.spinner("Polling target wallet..."):
        result = run_poll()
        st.session_state.last_poll_ts = time.time()
        st.session_state.last_poll_msg = result

if "last_poll_msg" in st.session_state:
    r = st.session_state.last_poll_msg
    cols = st.columns(4)
    cols[0].metric("Fetched", r["fetched"])
    cols[1].metric("New activity", r["new_activity"])
    cols[2].metric("Copied", r["copied"])
    cols[3].metric("Skipped", r["skipped"])
    if r["errors"]:
        for err in r["errors"][:5]:
            st.warning(err)

# ---------------------------------------------------------------------------
# Portfolio (paper)
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Your portfolio (paper)")

balance = get_paper_balance()
positions = get_paper_positions()

c1, c2, c3 = st.columns(3)
c1.metric("Paper balance", f"${balance:,.2f}")
c2.metric("Open positions", len(positions))
total_cost = sum(p.get("cost", 0) for p in positions.values())
c3.metric("Deployed", f"${total_cost:,.2f}")

if positions:
    rows = []
    for token_id, pos in positions.items():
        rows.append({
            "Market": pos.get("market", ""),
            "Outcome": pos.get("outcome", ""),
            "Token": token_id[:16] + "...",
            "Shares": round(pos["size"], 4),
            "Entry": round(pos["entry"], 4),
            "Cost ($)": round(pos["cost"], 2),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.info("No open paper positions.")

# ---------------------------------------------------------------------------
# Target wallet activity (trades + redeems)
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Target wallet activity (trades & redeems)")

df_target = load_target_activity(limit=150)
if df_target.empty:
    st.info("No target activity yet. Click **Poll now** or enable **Auto refresh**.")
else:
    type_filter = st.multiselect(
        "Filter by type",
        options=sorted(df_target["Type"].dropna().unique()),
        default=sorted(df_target["Type"].dropna().unique()),
    )
    display = df_target[df_target["Type"].isin(type_filter)] if type_filter else df_target
    st.dataframe(display, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Our copy trades
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Our copy executions")

df_copies = load_copy_trades(limit=100)
if df_copies.empty:
    st.info("No copy trades yet.")
else:
    mode_filter = st.multiselect(
        "Filter by mode",
        options=sorted(df_copies["Mode"].dropna().unique()),
        default=sorted(df_copies["Mode"].dropna().unique()),
    )
    display_copies = (
        df_copies[df_copies["Mode"].isin(mode_filter)] if mode_filter else df_copies
    )
    st.dataframe(display_copies, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Auto refresh (opt-in only)
# ---------------------------------------------------------------------------
if auto_poll:
    time.sleep(2)
    st.rerun()
