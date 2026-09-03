from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
STATE = (ROOT / "app" / "main_state.py").read_text(encoding="utf-8")


def test_dashboard_has_separate_bull_and_bear_tables():
    assert 'id="bullBody"' in INDEX
    assert 'id="bearBody"' in INDEX
    assert "Bullish Signals" in INDEX
    assert "Bearish Signals" in INDEX


def test_intraday_columns_are_present_in_required_order():
    needles = [
        'data-mtf="5min"',
        'data-mtf="15min"',
        'data-mtf="30min"',
        'data-mtf="1h"',
        'data-mtf="4h"',
    ]
    positions = [INDEX.index(n) for n in needles]
    assert positions == sorted(positions)


def test_mtf_matrix_is_memory_backed_and_arrow_enabled():
    assert '@app.get("/api/intraday-matrix")' in MAIN
    assert "LATEST_SYMBOL_STATES" in STATE
    assert "/api/intraday-matrix" in INDEX
    assert "▲ BUY / bullish" in INDEX
    assert "▼ SELL / bearish" in INDEX


def test_change_columns_precede_rsi_adx_rvol_and_include_2d_4d():
    header = INDEX[INDEX.index('<th>Symbol</th>'):INDEX.index('</tr></thead>')]
    needles = ['<th>1D%</th>', '<th>2D%</th>', '<th>4D%</th>', '<th>7D%</th>', '<th>30D%</th>', '<th>RSI</th>', '<th>ADX</th>', '<th>RVOL</th>']
    positions = [header.index(n) for n in needles]
    assert positions == sorted(positions)


def test_dashboard_table_can_show_all_237_signals_by_default():
    config_text = (ROOT / 'app' / 'config.py').read_text(encoding='utf-8')
    env_text = (ROOT / '.env.example').read_text(encoding='utf-8')
    assert 'TOP_ROWS", 237' in config_text
    assert 'TOP_ROWS=237' in env_text
