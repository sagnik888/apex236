import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime
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
    tp1 = Column(Float, nullable=True)
    tsl = Column(Float, nullable=True)

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
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)

Base.metadata.create_all(engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
