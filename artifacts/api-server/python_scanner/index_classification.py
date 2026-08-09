"""NSE index classification for the scan universe.

Lets the operator scan by index tier — Nifty 50, Next 50, Midcap 150, Smallcap
250 — individually or in any combination, instead of always scanning all 236.

Symbols here are EXCHANGE tickers, which are not always the familiar name. Three
in particular were supplied under obsolete or informal names and are corrected
below against the live Upstox instrument master:

    VISHALMART -> VMM          "VISHAL MEGA MART LIMITED"
    WAAREE     -> WAAREEENER   "WAAREE ENERGIES LIMITED"
    PEL        -> PIRAMALFIN   "PIRAMAL FINANCE LIMITED"

TATAMOTORS is obsolete after the 2025 restructuring: the commercial-vehicle
company is TMCV (Next 50) and the passenger-vehicle company is TMPV (Nifty 50).
Both are real NSE tickers; neither is currently in the 236-symbol scan universe,
so TMCV is listed here for correctness but reports as not-scannable rather than
being silently dropped.

Membership is a point-in-time snapshot (Nifty 50 / Next 50 as of Jul-2026,
Midcap 150 / Smallcap 250 replication portfolios as of 30-Jun-2026). NSE
rebalances semi-annually — treat this as data to refresh, not a constant.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJAJFINSV", "BAJFINANCE", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN", "JSWSTEEL",
    "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TATACONSUM", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

NEXT50 = [
    "ABB", "ADANIENSOL", "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "BAJAJHLDNG",
    "BANKBARODA", "BOSCHLTD", "BPCL", "BRITANNIA", "CANBK", "CGPOWER", "CHOLAFIN",
    "CUMMINSIND", "DIVISLAB", "DLF", "DMART", "GAIL", "GODREJCP", "HAL", "HDFCAMC",
    "HINDZINC", "HYUNDAI", "INDHOTEL", "IOC", "IRFC", "JINDALSTEL", "LODHA", "LTM",
    "MAZDOCK", "MOTHERSON", "MUTHOOTFIN", "PFC", "PIDILITIND", "PNB", "RECLTD",
    "SHREECEM", "SIEMENS", "SOLARINDS", "TMCV", "TATAPOWER", "TORNTPHARM", "TVSMOTOR",
    "UNIONBANK", "VBL", "VEDL", "ZYDUSLIFE",
]

MIDCAP150 = [
    "360ONE", "ABBOTINDIA", "ABCAPITAL", "ACC", "ALKEM", "APLAPOLLO", "ASHOKLEY",
    "ASTRAL", "AUBANK", "AUROPHARMA", "BALKRISIND", "BANKINDIA", "BDL", "BERGEPAINT",
    "BHARATFORG", "BHEL", "BIOCON", "BLUESTARCO", "BSE", "COCHINSHIP", "COFORGE",
    "COLPAL", "CONCOR", "COROMANDEL", "DABUR", "DALBHARAT", "DIXON", "EXIDEIND",
    "FEDERALBNK", "FORTIS", "GLENMARK", "GMRAIRPORT", "GODFRYPHLP", "GODREJPROP",
    "HAVELLS", "HEROMOTOCO", "HINDPETRO", "HUDCO", "ICICIGI", "ICICIPRULI", "IDEA",
    "IDFCFIRSTB", "INDIANB", "INDUSINDBK", "INDUSTOWER", "IPCALAB", "IRCTC", "IREDA",
    "JSL", "JSWENERGY", "JUBLFOOD", "KALYANKJIL", "KEI", "KPITTECH", "LAURUSLABS",
    "LICHSGFIN", "LICI", "LTF", "LTTS", "LUPIN", "MANKIND", "MARICO", "MCX", "MFSL",
    "MOTILALOFS", "MPHASIS", "MRF", "NAM-INDIA", "NATIONALUM", "NAUKRI", "NHPC",
    "NMDC", "NYKAA", "OBEROIRLTY", "OFSS", "OIL", "PAGEIND", "PATANJALI", "PAYTM",
    "PERSISTENT", "PETRONET", "PHOENIXLTD", "PIIND", "POLICYBZR", "POLYCAB",
    "POWERINDIA", "PREMIERENE", "PRESTIGE", "RVNL", "SAIL", "SBICARD", "SJVN", "SRF",
    "SUPREMEIND", "SUZLON", "SWIGGY", "TATAELXSI", "TIINDIA", "TORNTPOWER", "UBL",
    "UPL", "VMM", "VOLTAS", "WAAREEENER", "YESBANK",
]

SMALLCAP250 = [
    "AARTIIND", "AMBER", "ANGELONE", "ATUL", "BALRAMCHIN", "BANDHANBNK", "BATAINDIA",
    "CAMS", "CANFINHOME", "CDSL", "CESC", "CHAMBLFERT", "CROMPTON", "DEEPAKNTR",
    "DELHIVERY", "FORCEMOT", "GRANULES", "IEX", "IGL", "INDIAMART", "INOXWIND",
    "KAYNES", "KFINTECH", "LALPATHLAB", "MANAPPURAM", "NBCC", "NCC", "NUVAMA",
    "PIRAMALFIN", "PGEL", "PNBHOUSING", "POONAWALLA", "RBLBANK", "SAMMAANCAP",
    "SONACOMS", "TATACHEM",
]

INDEX_GROUPS: dict[str, list[str]] = {
    "NIFTY50": NIFTY50,
    "NEXT50": NEXT50,
    "MIDCAP150": MIDCAP150,
    "SMALLCAP250": SMALLCAP250,
}

INDEX_LABELS: dict[str, str] = {
    "NIFTY50": "Nifty 50",
    "NEXT50": "Nifty Next 50",
    "MIDCAP150": "Nifty Midcap 150",
    "SMALLCAP250": "Nifty Smallcap 250",
}

ALL_INDICES = list(INDEX_GROUPS)

# Informal or obsolete names an operator might type, mapped to exchange tickers.
SYMBOL_ALIASES: dict[str, str] = {
    "VISHALMART": "VMM",
    "WAAREE": "WAAREEENER",
    "PEL": "PIRAMALFIN",
    "TATAMOTORS": "TMCV",      # post-2025 split: commercial vehicles
}

_SYMBOL_TO_INDEX: dict[str, str] = {
    sym: idx for idx, syms in INDEX_GROUPS.items() for sym in syms
}


def _bare(symbol: str) -> str:
    s = str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    return SYMBOL_ALIASES.get(s, s)


def classify(symbol: str) -> Optional[str]:
    """Index tier for a symbol, or None if it is not classified."""
    return _SYMBOL_TO_INDEX.get(_bare(symbol))


def normalise_indices(indices: Optional[Iterable[str]]) -> list[str]:
    """Clean an operator selection. Empty/unknown selection means ALL tiers.

    Failing open matters here: a typo in the settings file must not silently
    reduce the scan universe to nothing.
    """
    if not indices:
        return list(ALL_INDICES)
    wanted = [str(i).upper().strip() for i in indices]
    valid = [i for i in ALL_INDICES if i in wanted]
    unknown = [i for i in wanted if i not in ALL_INDICES]
    if unknown:
        logger.warning("Ignoring unknown index selection: %s", unknown)
    return valid or list(ALL_INDICES)


def symbols_for(indices: Optional[Iterable[str]] = None,
                universe: Optional[Iterable[str]] = None) -> list[str]:
    """Scan symbols for the selected tiers, intersected with the live universe.

    Returns symbols in the universe's own form (with the .NS suffix it uses),
    preserving the universe's ordering so downstream partitioning stays stable.
    """
    selected = set()
    for idx in normalise_indices(indices):
        selected.update(INDEX_GROUPS[idx])

    if universe is None:
        from nifty50 import NIFTY236_SYMBOLS
        universe = NIFTY236_SYMBOLS
    return [s for s in universe if _bare(s) in selected]


def coverage_report(universe: Optional[Iterable[str]] = None) -> dict:
    """Which universe symbols are classified, and which classified symbols are
    not scannable. Surfaced via the API so drift after an NSE rebalance is
    visible rather than silent."""
    if universe is None:
        from nifty50 import NIFTY236_SYMBOLS
        universe = NIFTY236_SYMBOLS
    uni = {_bare(s) for s in universe}
    classified = set(_SYMBOL_TO_INDEX)
    return {
        "universe_size": len(uni),
        "classified": len(classified),
        "unclassified_in_universe": sorted(uni - classified),
        "classified_not_in_universe": sorted(classified - uni),
        "counts": {
            idx: {
                "total": len(syms),
                "scannable": sum(1 for s in syms if s in uni),
                "label": INDEX_LABELS[idx],
            }
            for idx, syms in INDEX_GROUPS.items()
        },
    }


def describe() -> list[dict]:
    """Payload for the settings UI toggle."""
    report = coverage_report()
    return [
        {
            "id": idx,
            "label": INDEX_LABELS[idx],
            "total": report["counts"][idx]["total"],
            "scannable": report["counts"][idx]["scannable"],
        }
        for idx in ALL_INDICES
    ]


# A duplicate across tiers would double-count a symbol in the scan universe.
_seen: dict[str, str] = {}
for _idx, _syms in INDEX_GROUPS.items():
    for _s in _syms:
        if _s in _seen:
            raise ValueError(f"{_s} appears in both {_seen[_s]} and {_idx}")
        _seen[_s] = _idx
