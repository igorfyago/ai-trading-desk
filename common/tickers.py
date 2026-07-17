"""The desk's ticker universe — one server-side source of truth.

Mirrors the dashboard watchlist (the house list). Agents may quote ANY of
these off the live tape; GEX dealer-positioning structure exists only for
the index complex.
"""

GEX_TICKERS = {"SPY", "QQQ", "IWM", "XSP"}

WATCHLIST = {
    # INDEX
    "SPY", "QQQ", "TQQQ", "ES1!", "VIX", "BTCUSD", "ETHUSD", "XSP",
    # MAGGY-7 0DTE
    "GOOGL", "SPCX", "META", "NVDA", "AMZN", "TSLA", "MSFT", "AAPL",
    # CORE
    "NOW", "PLTR",
    # SWITCH
    "NBIS", "BE", "WYFI", "CIFR", "RDW", "HIMS", "IREN", "MRVL", "CEVA",
    "RGTI", "WULF", "POET", "BBAI", "ORCL", "INTC", "MU", "ONDS", "RUN",
    "MSTR", "FIG", "NFLX", "IBM", "ACN",
    # COMMONS
    "ASTS", "SNDK", "OKLO", "CLSK", "HOOD", "ASST", "AMKR", "RIOT", "AG",
    "OSCR", "CRWV", "AMD", "IAG", "ZETA", "COIN", "RIVN", "OPEN", "SOFI",
    "GFI", "LMND", "VG", "BA", "NIO", "CELH", "BABA", "IWM", "BRK.B", "XOM",
    # ON DECK
    "RKLB", "JOBY", "VKTX", "GTLB", "UNH", "COST",
    # RADAR
    "MARA", "ARM", "ARKG", "JD",
    # OTHER
    "SOXL", "SMCI", "MP", "CRCL", "COHR", "RCAT", "TEM", "IONQ", "LITE",
    "SBET", "SPCE", "CAT", "VRT", "TWLO", "BITX", "BMNR", "ANET", "CBRS",
    "DUOL", "TSLL", "SLNH", "OXY", "IBEX35", "US10Y", "ORR", "DXC", "LLY",
    "CG", "BIDU", "NVO", "XPEV",
}

# Tickers that collide with everyday English — match these only when the
# text clearly means the ticker (written in CAPS, or $-prefixed).
AMBIGUOUS = {"NOW", "BE", "RUN", "OPEN", "CAT", "ARM", "COIN", "AG", "VG",
             "MU", "ALL", "IBM", "COST", "FIG", "JD", "CG", "MP", "BA"}
