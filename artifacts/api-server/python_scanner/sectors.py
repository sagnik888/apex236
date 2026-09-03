def get_sector(symbol: str) -> str:
    s = symbol.upper().replace(".NS", "").replace(".BO", "")
    if "BANK" in s or s in ("HDFC", "SBIN", "ICICI", "AXIS", "KOTAK", "INDUSINDBK", "PNB", "BOB", "CANBK", "AUBANK", "FEDERALBNK", "IDFCFIRSTB", "BANDHANBNK", "INDIANB"): 
        return "BANK"
    if s in ("TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", "MPHASIS", "COFORGE", "PERSISTENT"): 
        return "IT"
    if "AUTO" in s or s in ("MARUTI", "M&M", "TATAMOTORS", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT", "TVSMOTOR", "ASHOKLEY", "BOSCHLTD", "SONACOMS"): 
        return "AUTO"
    if "PHARMA" in s or s in ("SUNPHARMA", "CIPLA", "DRREDDY", "DIVISLAB", "LUPIN", "AUROPHARMA", "ZYDUSLIFE", "TORNTPHARM", "ALKEM", "BIOCON"): 
        return "PHARMA"
    if s in ("RELIANCE", "ONGC", "NTPC", "POWERGRID", "COALINDIA", "IOC", "BPCL", "TATAPOWER", "ADANIGREEN", "ADANIENSOL"): 
        return "ENERGY"
    if s in ("TATASTEEL", "HINDALCO", "JSWSTEEL", "VEDL", "JINDALSTEL", "NMDC"): 
        return "METAL"
    if s in ("ITC", "HINDUNILVR", "NESTLEIND", "BRITANNIA", "TATACONSUM", "DABUR", "GODREJCP", "MARICO", "COLPAL", "VBL", "UBL"): 
        return "FMCG"
    if "FIN" in s or s in ("BAJFINANCE", "BAJAJFINSV", "CHOLAFIN", "MUTHOOTFIN", "SHRIRAMFIN", "PFC", "RECLTD", "IREDA", "ABCAPITAL", "NUVAMA"): 
        return "FINANCE"
    if s in ("LT", "ULTRACEMCO", "GRASIM", "AMBUJACEM", "SHREECEM", "ACC", "DLF", "LODHA", "PRESTIGE", "GODREJPROP", "OBEROIRLTY", "MACROTECH"): 
        return "INFRA/REALTY"
    if s in ("TITAN", "TRENT", "ZOMATO", "NYKAA", "PAYTM", "PBFINTECH", "DELHIVERY", "VMM"):
        return "CONSUMER/TECH"
    if s in ("ADANIENT", "ADANIPORTS", "BHEL", "BEL", "HAL", "BDL", "MAZDOCK", "COCHINSHIP"):
        return "DEFENSE/INDUSTRIALS"
    if s in ('BHARTIARTL', 'IDEA', 'INDUSTOWER', 'TATACOMM'): return 'TELECOM'
    if s in ('ASIANPAINT', 'BERGEPAINT', 'KANSAINER', 'PIDILITIND', 'SRF', 'AARTIIND', 'DEEPAKNTR', 'NAVINFLUOR', 'PIIND', 'TATACHEM', 'UPL', 'COROMANDEL'): return 'CHEMICALS'
    if s in ('APOLLOHOSP', 'MAXHEALTH', 'FORTIS', 'METROPOLIS', 'LALPATHLAB', 'SYNGENE'): return 'HEALTHCARE'
    if s in ('INDIGO', 'IRCTC', 'CONCOR', 'MOTHERSON', 'BOSCHLTD', 'MRF', 'APOLLOTYRE', 'CEATLTD', 'BALKRISIND'): return 'LOGISTICS_AUTOANC'
    if s in ('PAGEIND', 'BATAINDIA', 'RELAXO', 'VOLTAS', 'HAVELLS', 'CROMPTON', 'DIXON', 'POLYCAB', 'KEI', 'AMBER', 'WHIRLPOOL'): return 'CONSUMER_DURABLES'
    if s in ('MCX', 'IEX', 'BSE', 'CDSL', 'CAMS', 'KFINTECH', 'UTIAMC', 'NAM-INDIA', 'HDFCAMC'): return 'CAPITAL_MARKETS'
    return 'OTHER_EQUITY'

