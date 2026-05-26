# Polymarket Copy Trading Bot

Streamlit app that monitors a target Polymarket wallet, shows their trades and redeems, and optionally copies them in **paper** or **real** mode.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set TARGET_WALLET at minimum
streamlit run app.py
```

## Features

- **Target activity feed** — all `TRADE` and `REDEEM` events with timestamps, market, outcome, side, price, shares, USDC value, token ID, tx hash
- **Paper trading** — simulated balance and positions persisted in SQLite
- **Real trading** — FOK market orders with slippage cap (requires `PRIVATE_KEY` + `ENABLE_REAL_TRADING=true`)
- **Refresh control** — auto refresh is **off** by default; use **Poll now** or enable **Auto refresh** in the sidebar

## Safety

- Real mode only runs when you select "Real trading" in the UI and credentials are valid
- Activity is marked processed only after a successful copy (failed real orders can be retried)
- Use a dedicated wallet with limited funds for live trading
