import json
from sqlalchemy import create_engine, text

from config import DATABASE_URL, STARTING_BALANCE

engine = create_engine(DATABASE_URL)


def init_db():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS target_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id TEXT UNIQUE,
                activity_type TEXT,
                timestamp TEXT,
                market TEXT,
                outcome TEXT,
                token_id TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                transaction_hash TEXT,
                raw_json TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS copy_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                mode TEXT,
                source_activity_id TEXT,
                market TEXT,
                token_id TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                pnl REAL,
                status TEXT,
                order_id TEXT,
                message TEXT
            )
        """))

        # Backfill columns for existing databases.
        cols = conn.execute(text("PRAGMA table_info(copy_trades)")).fetchall()
        col_names = {c[1] for c in cols}
        if "outcome" not in col_names:
            conn.execute(text("ALTER TABLE copy_trades ADD COLUMN outcome TEXT"))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS processed_activity (
                activity_id TEXT PRIMARY KEY,
                processed_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """))
        row = conn.execute(
            text("SELECT value FROM app_state WHERE key = 'paper_balance'")
        ).fetchone()
        if row is None:
            conn.execute(
                text("INSERT INTO app_state (key, value) VALUES ('paper_balance', :v)"),
                {"v": str(STARTING_BALANCE)},
            )
        row = conn.execute(
            text("SELECT value FROM app_state WHERE key = 'paper_positions'")
        ).fetchone()
        if row is None:
            conn.execute(
                text(
                    "INSERT INTO app_state (key, value) VALUES ('paper_positions', :v)"
                ),
                {"v": "{}"},
            )


def get_state(key: str, default: str = "") -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM app_state WHERE key = :k"), {"k": key}
        ).fetchone()
    return row[0] if row else default


def set_state(key: str, value: str):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO app_state (key, value) VALUES (:k, :v)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """),
            {"k": key, "v": value},
        )


def get_paper_balance() -> float:
    return float(get_state("paper_balance", str(STARTING_BALANCE)))


def set_paper_balance(balance: float):
    set_state("paper_balance", str(balance))


def get_paper_positions() -> dict:
    raw = get_state("paper_positions", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def set_paper_positions(positions: dict):
    set_state("paper_positions", json.dumps(positions))


def get_processed_ids() -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT activity_id FROM processed_activity")).fetchall()
    return {r[0] for r in rows}


def mark_processed(activity_id: str, processed_at: str):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT OR IGNORE INTO processed_activity (activity_id, processed_at)
                VALUES (:id, :ts)
            """),
            {"id": activity_id, "ts": processed_at},
        )


def is_processed(activity_id: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM processed_activity WHERE activity_id = :id"),
            {"id": activity_id},
        ).fetchone()
    return row is not None
