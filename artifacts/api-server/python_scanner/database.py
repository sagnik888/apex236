import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class Trade(Base):
    __tablename__ = 'trades'

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, nullable=False, index=True)
    timeframe = Column(String, nullable=False, index=True)
    direction = Column(String, nullable=False)
    entry_time = Column(DateTime, nullable=False)
    entry_price = Column(Float, nullable=False)
    
    # Active, Closed, Repaint_Exit
    status = Column(String, nullable=False, default="ACTIVE")
    
    sl1 = Column(Float, nullable=True)
    sl2 = Column(Float, nullable=True)
    tp1 = Column(Float, nullable=True)
    tp2 = Column(Float, nullable=True)
    tp3 = Column(Float, nullable=True)
    tsl = Column(Float, nullable=True)

    # Immutable/continuing APEX state.  These fields are not an independent
    # execution model: they are a durable snapshot of the scanner's own
    # ActiveTrade so a later scan, UI view, or broker adapter sees the same
    # levels and provenance.
    signal_time = Column(DateTime, nullable=True)
    entry_bar = Column(Integer, nullable=True)
    setup = Column(String, nullable=True)
    stop_mode = Column(String, nullable=True)
    option_type = Column(String, nullable=True)
    option_strike = Column(Float, nullable=True)
    t1_hit = Column(Boolean, nullable=False, default=False)
    t2_hit = Column(Boolean, nullable=False, default=False)
    t3_hit = Column(Boolean, nullable=False, default=False)
    profit_locked = Column(Boolean, nullable=False, default=False)
    peak_price = Column(Float, nullable=True)
    trough_price = Column(Float, nullable=True)
    exit_confirmation_count = Column(Integer, nullable=False, default=0)

    exit_time = Column(DateTime, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)
    pnl = Column(Float, nullable=True)
    
    trade_type = Column(String, nullable=True)

class SignalState(Base):
    __tablename__ = 'signal_states'

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, nullable=False, index=True)
    timeframe = Column(String, nullable=False, index=True)
    signal_dir = Column(String, nullable=False)
    first_seen_time = Column(DateTime, nullable=False)
    is_executed = Column(Boolean, default=False)
    # The bar timestamp of the signal to ensure we don't carry over states across candles
    candle_timestamp = Column(DateTime, nullable=False)

# Create engine
DB_PATH = os.path.join(os.path.dirname(__file__), "apex_trading.db")
engine = create_engine(
    f"sqlite:///{DB_PATH}?timeout=30",
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=False,
)

Base.metadata.create_all(engine)


def _migrate_sqlite_schema() -> None:
    """Add APEX state columns for installs created before this release.

    ``create_all`` never alters an existing SQLite table.  Keeping this tiny,
    idempotent migration here avoids silently retaining the old truncated
    trade state on desktop installs.
    """
    import time
    additions = {
        "sl2": "FLOAT", "tp2": "FLOAT", "tp3": "FLOAT",
        "signal_time": "DATETIME", "entry_bar": "INTEGER",
        "setup": "VARCHAR", "stop_mode": "VARCHAR", "option_type": "VARCHAR",
        "option_strike": "FLOAT", "t1_hit": "BOOLEAN NOT NULL DEFAULT 0",
        "t2_hit": "BOOLEAN NOT NULL DEFAULT 0", "t3_hit": "BOOLEAN NOT NULL DEFAULT 0",
        "profit_locked": "BOOLEAN NOT NULL DEFAULT 0", "peak_price": "FLOAT",
        "trough_price": "FLOAT", "exit_confirmation_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for attempt in range(5):
        try:
            with engine.begin() as conn:
                try:
                    conn.execute(text("PRAGMA journal_mode=WAL;"))
                except Exception:
                    pass
                existing = {row[1] for row in conn.execute(text("PRAGMA table_info(trades)"))}
                for name, definition in additions.items():
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE trades ADD COLUMN {name} {definition}"))
            break
        except Exception as exc:
            if attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))


_migrate_sqlite_schema()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
