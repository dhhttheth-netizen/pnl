# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════════════════
#  NSE SHORT TRADE SCANNER  —  UPSTOX 1-MINUTE API VERSION  (FIXED)
# ═══════════════════════════════════════════════════════════════════════════

import sys, os, gzip, io, warnings, time as time_module
from datetime import date, datetime, timedelta, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse
import difflib

print("=" * 65)
print("  NSE SHORT SCANNER  —  Upstox 1-Minute API  (FIXED + MCAP)")
print("=" * 65)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

print("Loading libraries ...", flush=True)
import requests
import pandas as pd

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False
    print("[WARN] yfinance not installed — market cap filter will be skipped.")
    print("       Run: pip install yfinance --break-system-packages")

warnings.filterwarnings("ignore")
print("Libraries loaded.\n", flush=True)

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════

ACCESS_TOKEN        = os.environ.get("UPSTOX_ACCESS_TOKEN", "")

CSV_PATH            = "5000.csv"
SCAN_DATE_STR       = os.environ.get("SCAN_DATE", "")  # blank = auto (yesterday-ish trading day)

WICK_TOP_N          = 5
GAINER_TOP_N        = 5
MIN_WINDOW_GAIN_PCT = 1.5
GAP_FILTER_MULT     = 0.98
CAPITAL_PER_TRADE   = 50_000

WINDOW_START        = "14:15"
WINDOW_END          = "15:30"

FETCH_WORKERS       = 10
FETCH_DELAY         = 0.05

FUZZY_SUGGEST_LIMIT = 30
DEBUG_RAW_SAMPLE    = False  # off by default for unattended runs (keeps logs cleaner)

APPLY_MCAP_FILTER   = True
MIN_MARKET_CAP_CR   = 5000
CRORE               = 10_000_000
MCAP_FETCH_WORKERS  = 80

YFINANCE_SYMBOL_OVERRIDES = {
    "J&KBANK"     : "JKBANK.NS",
    "ARE&M"       : "AREIM.NS",
    "M&M"         : "M&M.NS",
    "M&MFIN"      : "M&MFIN.NS",
    "L&TFH"       : "L&TFH.NS",
    "GICRE"       : "GICHOUSING.NS",
    "SOLARINDS"   : "SOLARINDUSTRIES.NS",
    "MASFIN"      : "MASFIN.NS",
    "DIVISLAB"    : "DIVISLAB.NS",
    "HDFCLIFE"    : "HDFCLIFE.NS",
    "TIINDIA"     : "TIINDIA.NS",
    "DEEPAKNTR"   : "DEEPAKNTR.NS",
    "HINDUNILVR"  : "HINDUNILVR.NS",
    "CUMMINSIND"  : "CUMMINSIND.NS",
    "ALKYLAMINE"  : "ALKYLAMINECHEM.NS",
    "BAYERCROP"   : "BAYERCROP.NS",
    "HAPPYFORGE"  : "HAPPYFORGE.NS",
    "EUREKAFORB"  : "EUREKAFORB.NS",
    "ZENTEC"      : "ZENTEC.NS",
    "VENTIVE"     : "VENTIVE.NS",
    "MANORAMA"    : "MANINDS.NS",
    "SKYGOLD"     : "SKYGOLD.NS",
    "MANYAVAR"    : "VEDANT.NS",
    "POLYCAB"     : "POLYCAB.NS",
    "APARINDS"    : "APARINDS.NS",
    "FORCEMOT"    : "FORCEMOT.NS",
    "BRITANNIA"   : "BRITANNIA.NS",
    "RELIANCE"    : "RELIANCE.NS",
    "HINDALCO"    : "HINDALCO.NS",
    "BEL"         : "BEL.NS",
    "IGL"         : "IGL.NS",
    "CESC"        : "CESC.NS",
    "ATUL"        : "ATUL.NS",
}

# ═══════════════════════════════════════════════════════════════════════════
#  DATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _skip_weekends(d: date, step: int) -> date:
    d += timedelta(days=step)
    while d.weekday() >= 5:
        d += timedelta(days=step)
    return d

def next_trading_day(d: date) -> date:
    return _skip_weekends(d, 1)

def prev_trading_day(d: date) -> date:
    return _skip_weekends(d, -1)

def get_scan_date() -> date:
    """
    Unattended-safe: NEVER calls input(). If SCAN_DATE env var is blank,
    defaults to the previous trading day relative to today (since this
    script scans an afternoon window to produce TOMORROW's shortlist —
    when run at 7am, "yesterday" is the most recent completed session).
    """
    if SCAN_DATE_STR.strip():
        return datetime.strptime(SCAN_DATE_STR.strip(), "%Y-%m-%d").date()
    return prev_trading_day(date.today())

# ═══════════════════════════════════════════════════════════════════════════
#  LOAD SYMBOLS
# ═══════════════════════════════════════════════════════════════════════════

def load_symbols(path: str) -> list:
    df  = pd.read_csv(path)
    col = df.columns[0]
    syms = (
        df[col].dropna().astype(str).str.strip().str.upper()
        .pipe(lambda s: s[s != ""])
        .tolist()
    )
    cleaned = [s.replace(".NSE", "").replace(".NS", "").strip() for s in syms]
    cleaned = [s for s in cleaned if s]
    print(f"[INFO] {len(cleaned)} symbols loaded from {path}", flush=True)
    return cleaned

# ═══════════════════════════════════════════════════════════════════════════
#  UPSTOX INSTRUMENT MASTER
# ═══════════════════════════════════════════════════════════════════════════

def fetch_instrument_master() -> dict:
    print("[MASTER] Downloading NSE instrument master ...", flush=True)
    url  = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"
    resp = requests.get(url, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Instrument master HTTP {resp.status_code}")
    with gzip.open(io.BytesIO(resp.content), "rt", encoding="utf-8") as f:
        df = pd.read_csv(f)

    eq      = df[df["instrument_key"].str.startswith("NSE_EQ|", na=False)].copy()
    sym_col = next(c for c in ["tradingsymbol", "trading_symbol", "symbol"] if c in eq.columns)
    key_col = next(c for c in ["instrument_key", "instrumentkey"] if c in eq.columns)

    master = {
        str(r[sym_col]).upper().strip(): str(r[key_col]).strip()
        for _, r in eq.iterrows()
        if str(r[sym_col]).upper().strip() not in ("", "NAN")
    }
    print(f"  -> {len(master)} NSE_EQ instruments loaded", flush=True)
    return master, sorted(master.keys())

# ═══════════════════════════════════════════════════════════════════════════
#  DIAGNOSTIC: REPORT SYMBOL <-> MASTER MISMATCHES
# ═══════════════════════════════════════════════════════════════════════════

def diagnose_symbol_mapping(symbols: list, master: dict, master_keys_sorted: list):
    matched   = [s for s in symbols if s in master]
    unmatched = [s for s in symbols if s not in master]

    print("\n" + "─" * 65)
    print("  SYMBOL <-> INSTRUMENT MASTER MAPPING CHECK")
    print("─" * 65)
    print(f"  Total symbols in your CSV : {len(symbols)}")
    print(f"  Matched in Upstox master  : {len(matched)}")
    print(f"  UNMATCHED (will be skipped, not queried with a bad key) : {len(unmatched)}")

    if unmatched:
        print(f"\n  First {min(20, len(unmatched))} unmatched symbols:")
        for s in unmatched[:20]:
            print(f"    - {s}")

        if FUZZY_SUGGEST_LIMIT > 0:
            print(f"\n  Fuzzy-match suggestions for first "
                  f"{min(FUZZY_SUGGEST_LIMIT, len(unmatched))} unmatched symbols "
                  f"(closest names actually in the master):")
            for s in unmatched[:FUZZY_SUGGEST_LIMIT]:
                close = difflib.get_close_matches(s, master_keys_sorted, n=3, cutoff=0.6)
                print(f"    {s:15s} -> {close if close else '(no close match found)'}")

    print("─" * 65 + "\n")
    return matched, unmatched

# ═══════════════════════════════════════════════════════════════════════════
#  MARKET CAP PRE-FILTER
# ═══════════════════════════════════════════════════════════════════════════

def _nse_to_yf_candidates(sym: str) -> list:
    sym_clean = sym.upper().strip()

    if sym_clean in YFINANCE_SYMBOL_OVERRIDES:
        override = YFINANCE_SYMBOL_OVERRIDES[sym_clean]
        candidates = [override]
        if not override.endswith(".NS"):
            candidates.append(sym_clean + ".NS")
        candidates.append(sym_clean + ".BO")
        return candidates

    candidates = []
    candidates.append(sym_clean + ".NS")
    candidates.append(sym_clean + ".BO")

    if "&" in sym_clean:
        replaced = sym_clean.replace("&", "and")
        candidates.append(replaced + ".NS")
        candidates.append(replaced + ".BO")
        replaced2 = sym_clean.replace("&", "-")
        candidates.append(replaced2 + ".NS")

    if "-" in sym_clean:
        replaced = sym_clean.replace("-", "")
        candidates.append(replaced + ".NS")

    for suffix in ["LTD", "LIMITED", "IND", "INDS", "FIN"]:
        if sym_clean.endswith(suffix):
            base = sym_clean[: -len(suffix)]
            candidates.append(base + ".NS")
            candidates.append(base + "S.NS")

    return list(dict.fromkeys(candidates))


def _try_fetch_market_cap(ticker_sym: str):
    try:
        t = yf.Ticker(ticker_sym)
        info = t.info
        if not info or info.get("quoteType") in (None, ""):
            return None
        mcap = info.get("marketCap")
        if mcap and mcap > 0:
            return float(mcap)
        shares = (
            info.get("sharesOutstanding")
            or info.get("impliedSharesOutstanding")
            or info.get("floatShares")
        )
        price = info.get("regularMarketPrice") or info.get("previousClose")
        if shares and price and shares > 0 and price > 0:
            return float(shares) * float(price)
        return None
    except Exception:
        return None


def fetch_market_caps(symbols: list) -> dict:
    print(f"\n[MCAP] Fetching market cap for {len(symbols)} symbols via yfinance ...", flush=True)
    print(f"[MCAP] Minimum required: Rs {MIN_MARKET_CAP_CR:,} Cr", flush=True)

    mcap_map = {}
    failed   = []

    def fetch_one(sym):
        for ticker_sym in _nse_to_yf_candidates(sym):
            mcap = _try_fetch_market_cap(ticker_sym)
            if mcap is not None:
                return sym, mcap
        return sym, None

    with ThreadPoolExecutor(max_workers=MCAP_FETCH_WORKERS) as ex:
        futures = {ex.submit(fetch_one, s): s for s in symbols}
        for fut in as_completed(futures):
            sym, mcap = fut.result()
            mcap_map[sym] = mcap
            if mcap is None:
                failed.append(sym)

    ok = len(symbols) - len(failed)
    print(f"[MCAP] Market cap fetched: {ok} OK  |  {len(failed)} failed (treated as ineligible)",
          flush=True)
    if failed:
        print(f"[MCAP] Failed symbols (first 30): {failed[:30]}{'...' if len(failed) > 30 else ''}",
              flush=True)

    return mcap_map


def report_mcap_eligibility(mcap_map: dict) -> set:
    eligible = set()
    rows = []
    for sym, mcap in mcap_map.items():
        if mcap is None:
            rows.append((sym, None, False))
            continue
        mcap_cr = mcap / CRORE
        is_elig = mcap_cr >= MIN_MARKET_CAP_CR
        if is_elig:
            eligible.add(sym)
        rows.append((sym, round(mcap_cr, 1), is_elig))

    total      = len(rows)
    elig_count = len(eligible)
    no_data    = sum(1 for _, mcap_cr, _ in rows if mcap_cr is None)
    below_min  = total - elig_count - no_data

    print("\n" + "─" * 65)
    print("  MARKET CAP ELIGIBILITY CHECK")
    print("─" * 65)
    print(f"  Minimum market cap required : Rs {MIN_MARKET_CAP_CR:,} Cr")
    print(f"  Total symbols checked        : {total}")
    print(f"  Eligible (>= min)            : {elig_count}")
    print(f"  Below minimum                : {below_min}")
    print(f"  No market cap data           : {no_data}")
    print("─" * 65 + "\n")

    return eligible

# ═══════════════════════════════════════════════════════════════════════════
#  FETCH 1-MINUTE CANDLES FOR ONE SYMBOL
# ═══════════════════════════════════════════════════════════════════════════

def fetch_candles_for_date(instrument_key: str, scan_date: date):
    date_str = scan_date.strftime("%Y-%m-%d")
    encoded  = urllib.parse.quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v2/historical-candle"
        f"/{encoded}/1minute/{date_str}/{date_str}"
    )
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept"       : "application/json",
    }

    for attempt in range(4):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code == 200:
                candles = r.json().get("data", {}).get("candles", [])
                if not candles:
                    return pd.DataFrame(), "no_candles_returned"
                df = pd.DataFrame(
                    candles,
                    columns=["timestamp", "Open", "High", "Low", "Close", "Volume", "OI"],
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                if df["timestamp"].dt.tz is None:
                    df["timestamp"] = df["timestamp"].dt.tz_localize("Asia/Kolkata")
                else:
                    df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
                df.set_index("timestamp", inplace=True)
                df.sort_index(inplace=True)
                df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
                df = df.between_time("09:00", "15:30")
                if df.empty:
                    return pd.DataFrame(), "empty_after_market_hours_filter"
                df = df.copy()
                df["_date"] = df.index.date
                df["_time"] = df.index.time
                return df, "ok"
            elif r.status_code == 429:
                time_module.sleep(3 + attempt * 2)
            elif r.status_code in (400, 401):
                return pd.DataFrame(), f"http_{r.status_code}_bad_key_or_auth"
            elif r.status_code == 404:
                return pd.DataFrame(), "http_404_not_found"
            else:
                time_module.sleep(1)
        except Exception:
            time_module.sleep(0.3)
    return pd.DataFrame(), "failed_after_retries"

# ═══════════════════════════════════════════════════════════════════════════
#  FETCH ALL SYMBOLS IN PARALLEL
# ═══════════════════════════════════════════════════════════════════════════

def fetch_all(symbols: list, master: dict, scan_date: date):
    print(f"\n[FETCH] 1-minute candles for {len(symbols)} MATCHED symbols on {scan_date} ...",
          flush=True)

    all_data   = {}
    fail_notes = {}
    counter    = {"n": 0}

    def _fetch_one(sym):
        ikey = master[sym]
        df, note = fetch_candles_for_date(ikey, scan_date)
        counter["n"] += 1
        if counter["n"] % 200 == 0:
            print(f"    ... {counter['n']}/{len(symbols)} fetched", flush=True)
        time_module.sleep(FETCH_DELAY)
        return sym, df, note

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futures = {ex.submit(_fetch_one, s): s for s in symbols}
        for fut in as_completed(futures):
            sym, df, note = fut.result()
            if df is not None and not df.empty:
                all_data[sym] = df
            else:
                fail_notes[sym] = note

    print(f"[FETCH] Complete — {len(all_data)} symbols with data, "
          f"{len(fail_notes)} symbols returned no data.\n", flush=True)

    if fail_notes:
        from collections import Counter
        reason_counts = Counter(fail_notes.values())
        print("[FETCH] Breakdown of why matched symbols still returned no data:")
        for reason, cnt in reason_counts.most_common():
            print(f"    {reason:35s} : {cnt}")
        print()

    return all_data, fail_notes

# ═══════════════════════════════════════════════════════════════════════════
#  SCAN 14:15 – 15:30 WINDOW
# ═══════════════════════════════════════════════════════════════════════════

def scan_window(all_data: dict, scan_date: date) -> pd.DataFrame:
    rows = []
    for sym, df in all_data.items():
        day_df = df[df["_date"] == scan_date]
        if day_df.empty:
            continue

        win = day_df.between_time(WINDOW_START, WINDOW_END)
        if win.empty or len(win) < 2:
            continue

        first_open = float(win["Open"].iat[0])
        last_close = float(win["Close"].iat[-1])
        max_high   = float(win["High"].max())
        day_close  = float(day_df["Close"].iat[-1])

        if first_open <= 0 or last_close <= 0:
            continue

        pct_gain       = (last_close - first_open) / first_open * 100
        upper_wick_pct = (max_high   - last_close) / last_close  * 100

        if last_close <= first_open:
            continue
        if pct_gain < MIN_WINDOW_GAIN_PCT:
            continue

        rows.append({
            "symbol"         : sym,
            "prev_close"     : round(day_close, 2),
            "win_open"       : round(first_open, 2),
            "win_close"      : round(last_close, 2),
            "win_high"       : round(max_high, 2),
            "pct_gain"       : round(pct_gain, 3),
            "upper_wick_pct" : round(upper_wick_pct, 3),
            "gap_skip_below" : round(day_close * GAP_FILTER_MULT, 2),
            "candles_in_win" : len(win),
        })

    return pd.DataFrame(rows)

# ═══════════════════════════════════════════════════════════════════════════
#  BUILD SHORTLIST
# ═══════════════════════════════════════════════════════════════════════════

def build_shortlist(scan_df: pd.DataFrame, mcap_eligible):
    if scan_df.empty:
        return pd.DataFrame(), set()

    if mcap_eligible is not None:
        before = len(scan_df)
        scan_df = scan_df[scan_df["symbol"].isin(mcap_eligible)].copy()
        after = len(scan_df)
        print(f"[MCAP] Qualifying stocks before MCap filter: {before}  ->  after: {after}")
        if scan_df.empty:
            print("[MCAP] No stocks left after market cap filter.")
            return pd.DataFrame(), set()

    wick_df             = scan_df.nlargest(WICK_TOP_N,   "upper_wick_pct").copy()
    gainer_df           = scan_df.nlargest(GAINER_TOP_N, "pct_gain").copy()
    wick_df["basket"]   = "WICK"
    gainer_df["basket"] = "GAINER"

    overlap  = set(wick_df["symbol"]) & set(gainer_df["symbol"])
    combined = pd.concat([wick_df, gainer_df], ignore_index=True)
    combined["both_baskets"] = combined["symbol"].apply(
        lambda s: "YES" if s in overlap else "")
    combined.sort_values(
        ["basket", "pct_gain"], ascending=[True, False], inplace=True)
    combined.reset_index(drop=True, inplace=True)
    combined.index += 1
    return combined, overlap

# ═══════════════════════════════════════════════════════════════════════════
#  MAIN  — unattended-safe (no input(), exits cleanly with status codes)
# ═══════════════════════════════════════════════════════════════════════════

def main():
    scan_date  = get_scan_date()
    trade_date = next_trading_day(scan_date)

    print(f"\n  Scan date  : {scan_date}  (14:15–15:30 window, 1-min bars)")
    print(f"  Trade date : {trade_date}  <- SHORT THESE STOCKS AT OPEN")
    print(f"  Config     : WICK top-{WICK_TOP_N} | GAINER top-{GAINER_TOP_N} | "
          f"Min gain {MIN_WINDOW_GAIN_PCT}% | Gap filter x{GAP_FILTER_MULT}")
    if APPLY_MCAP_FILTER:
        print(f"  MCap filter: >= Rs {MIN_MARKET_CAP_CR:,} Cr  (applied before top-N ranking)")
    print("─" * 65)

    if not ACCESS_TOKEN:
        print("\n[ERROR] No ACCESS_TOKEN set.")
        print("        UPSTOX_ACCESS_TOKEN environment variable is empty.")
        sys.exit(1)

    if not os.path.exists(CSV_PATH):
        print(f"\n[ERROR] '{CSV_PATH}' not found.")
        sys.exit(1)

    symbols = load_symbols(CSV_PATH)
    master, master_keys_sorted = fetch_instrument_master()
    matched, unmatched = diagnose_symbol_mapping(symbols, master, master_keys_sorted)

    if not matched:
        print("[ERROR] None of your symbols matched the Upstox instrument master.")
        sys.exit(1)

    all_data, fail_notes = fetch_all(matched, master, scan_date)
    if not all_data:
        print("[ERROR] No candle data fetched for any matched symbol.")
        print("        Check ACCESS_TOKEN validity/expiry and that market was open on the scan date.")
        sys.exit(1)

    print(f"[SCAN] Analysing {WINDOW_START}–{WINDOW_END} window on {scan_date} ...", flush=True)
    scan_df = scan_window(all_data, scan_date)

    if scan_df.empty:
        print(f"\n[RESULT] No stocks passed filters on {scan_date}. Nothing to short on {trade_date}.")
        # Write an empty-but-valid shortlist so the dashboard shows
        # "no positions" cleanly instead of erroring on a missing file.
        pd.DataFrame(columns=["symbol", "basket"]).to_csv("shortlist_latest.csv", index=False)
        print("[OUTPUT] Wrote empty shortlist_latest.csv")
        return

    print(f"[SCAN] {len(scan_df)} stocks passed (gain > {MIN_WINDOW_GAIN_PCT}% & bullish in window)\n")

    mcap_eligible = None
    if APPLY_MCAP_FILTER:
        if not HAS_YFINANCE:
            print("\n[MCAP] yfinance not installed — skipping market cap filter.")
        else:
            qualifying_syms = sorted(scan_df["symbol"].unique())
            mcap_map      = fetch_market_caps(qualifying_syms)
            mcap_eligible = report_mcap_eligibility(mcap_map)

    shortlist, overlap = build_shortlist(scan_df, mcap_eligible)

    if shortlist.empty:
        print(f"\n[RESULT] No stocks passed filters (incl. market cap) on {scan_date}.")
        pd.DataFrame(columns=["symbol", "basket"]).to_csv("shortlist_latest.csv", index=False)
        print("[OUTPUT] Wrote empty shortlist_latest.csv")
        return

    print("\n" + "=" * 65)
    print(f"  SHORTLIST — SHORT ON {trade_date}  ({len(shortlist)} signals)")
    print("=" * 65)
    print(f"  WICK   : {sorted(set(shortlist[shortlist['basket']=='WICK']['symbol']))}")
    print(f"  GAINER : {sorted(set(shortlist[shortlist['basket']=='GAINER']['symbol']))}")
    if overlap:
        print(f"\n  * OVERLAP — appears in BOTH baskets: {sorted(overlap)}")

    # ── Output: ONLY symbol + basket columns, matching what main.py expects ──
    out_file = "shortlist_latest.csv"
    save_df  = shortlist[["symbol", "basket"]].copy()
    save_df.to_csv(out_file, index=False)
    print(f"\n[OUTPUT] Saved -> {out_file}  ({len(save_df)} rows)")
    print("[DONE]\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        print("\n[FATAL ERROR]", flush=True)
        traceback.print_exc()
        sys.exit(1)
