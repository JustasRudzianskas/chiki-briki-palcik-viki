import os
from dotenv import load_dotenv

load_dotenv()

TARGET_WALLET = os.getenv("TARGET_WALLET", "").strip()
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "10000"))

ENABLE_REAL_TRADING = os.getenv("ENABLE_REAL_TRADING", "false").lower() == "true"

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
RELAYER_API_KEY = os.getenv("RELAYER_API_KEY", "")
RELAYER_API_SECRET = os.getenv("RELAYER_API_SECRET", "")
RELAYER_API_PASSPHRASE = os.getenv("RELAYER_API_PASSPHRASE", "")

CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"
DATA_API_HOST = "https://data-api.polymarket.com"

COPY_TRADE_SIZE_USD = float(os.getenv("COPY_TRADE_SIZE_USD", "5"))
MAX_SLIPPAGE = float(os.getenv("MAX_SLIPPAGE", "0.02"))
MAX_POSITION_PER_MARKET = float(os.getenv("MAX_POSITION_PER_MARKET", "25"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///trades.db")
