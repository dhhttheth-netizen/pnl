# main.py — Live PnL tracker for your shortlisted SHORT candidates
# ─────────────────────────────────────────────────────────────────
#   pip install fastapi uvicorn pandas yfinance python-multipart
#   uvicorn main:app --reload --port 8000
#
# No broker login required anywhere. Reads your shortlist CSV
# (e.g. shortlist_2026-06-24.csv) and tracks live mark-to-market PnL
# for each symbol using yfinance, assuming a SHORT entry at TODAY's
# market open price (matches your strategy's actual entry logic:
# short at open, exit later).
#
# Env vars (optional, sensible defaults shown):
#   $env:SHORTLIST_FILE     = "shortlist_2026-06-24.csv"
#   $env:CAPITAL_PER_TRADE  = "50000"
#
# Cloud deploy (Render free tier): the platform injects $PORT — the start
# command in render.yaml binds to it automatically, no code change needed.
# ─────────────────────────────────────────────────────────────────
import os, json, asyncio, traceback
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import pandas as pd
import yfinance as yf
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Shortlist Live PnL Tracker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SHORTLIST_FILE    = os.environ.get("SHORTLIST_FILE", "shortlist_latest.csv")
CAPITAL_PER_TRADE = float(os.environ.get("CAPITAL_PER_TRADE", "50000"))

IST = ZoneInfo("Asia/Kolkata")

# Cache of today's open price per symbol, captured once and reused all day
# (so PnL doesn't drift if open price briefly comes back as None mid-session)
_entry_cache: dict[str, float] = {}

# Cache of the last real exception seen per symbol, for /api/debug
_last_error: dict[str, str] = {}


def market_open() -> bool:
    # Cloud hosts (Render, etc.) run their clock in UTC. NSE hours are in IST,
    # so this must be evaluated in IST regardless of the server's local timezone.
    now_ist = datetime.now(IST).time()
    return dtime(9, 15) <= now_ist <= dtime(15, 30)


def load_shortlist() -> pd.DataFrame:
    """
    Loads the shortlist CSV and collapses repeated symbols into ONE row,
    but remembers how many times each symbol appeared (occurrences).
    A symbol listed twice (e.g. once in WICK, once in GAINER) becomes a
    single position whose size is doubled — see compute_live_pnl().
    """
    if not os.path.exists(SHORTLIST_FILE):
        return pd.DataFrame()
    df = pd.read_csv(SHORTLIST_FILE)
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()

    if "basket" not in df.columns:
        df["basket"] = ""

    grouped = (
        df.groupby("symbol", sort=False)
          .agg(
              basket=("basket", lambda s: "+".join(sorted(set(str(x) for x in s if str(x).strip())))),
              occurrences=("symbol", "count"),
          )
          .reset_index()
    )
    return grouped


def _yf_symbol(sym: str) -> str:
    sym = str(sym).upper().strip()
    return sym if sym.endswith((".NS", ".BO")) else sym + ".NS"


def get_open_and_ltp(yf_sym: str) -> tuple[float | None, float | None]:
    """
    Returns (today_open, last_price) for a given yfinance symbol.
    Today's open is cached once fetched (your short entry price).
    Any real exception is stashed in _last_error[yf_sym] instead of being
    silently swallowed, so /api/debug/{symbol} can show you what's actually
    going wrong (rate limiting, blocked IP, bad symbol, etc.) instead of a
    generic "price unavailable" note.
    """
    try:
        t = yf.Ticker(yf_sym)

        # LTP — fast_info first, fallback to 1m history
        ltp = None
        try:
            ltp = t.fast_info.get("last_price")
        except Exception as e:
            _last_error[yf_sym] = f"fast_info(last_price) failed: {e!r}"

        if ltp is None:
            hist = t.history(period="1d", interval="1m")
            if not hist.empty:
                ltp = float(hist["Close"].iloc[-1])
            else:
                _last_error[yf_sym] = "history(1d, 1m) returned empty dataframe"

        # Today's open — cache once found
        today_open = _entry_cache.get(yf_sym)
        if today_open is None:
            try:
                today_open = t.fast_info.get("open")
            except Exception as e:
                _last_error[yf_sym] = f"fast_info(open) failed: {e!r}"
                today_open = None
            if today_open is None:
                hist = t.history(period="1d", interval="1m")
                if not hist.empty:
                    today_open = float(hist["Open"].iloc[0])
            if today_open:
                _entry_cache[yf_sym] = today_open
                _last_error.pop(yf_sym, None)  # clear stale error once it works

        if ltp is not None and today_open is not None:
            _last_error.pop(yf_sym, None)

        return today_open, ltp
    except Exception as e:
        _last_error[yf_sym] = f"unhandled: {e!r}\n{traceback.format_exc()}"
        return None, None


def compute_live_pnl() -> dict:
    df = load_shortlist()
    if df.empty:
        return {
            "today_pnl": 0.0,
            "positions": [],
            "market_open": market_open(),
            "error": f"{SHORTLIST_FILE} not found or empty",
            "ts": datetime.now(IST).isoformat(),
        }

    positions = []
    total_pnl = 0.0

    for _, row in df.iterrows():
        sym         = str(row["symbol"])
        basket      = row.get("basket", "")
        occurrences = int(row.get("occurrences", 1))
        yf_sym      = _yf_symbol(sym)

        entry_price, ltp = get_open_and_ltp(yf_sym)

        if entry_price is None or ltp is None or entry_price <= 0:
            positions.append({
                "symbol": sym, "basket": basket,
                "entry_price": entry_price, "ltp": ltp,
                "qty": None, "pnl": 0.0,
                "pnl_pct": None,
                "occurrences": occurrences,
                "note": "price unavailable (market may not be open yet, or symbol mismatch) "
                        f"— see /api/debug/{sym} for the real error",
            })
            continue

        base_qty = max(1, int(CAPITAL_PER_TRADE // entry_price))
        qty      = base_qty * occurrences  # doubled (or more) if symbol appeared multiple times
        # SHORT position: profit when price falls below entry
        pnl_pct = round((entry_price - ltp) / entry_price * 100, 3)
        pnl_rs  = round((entry_price - ltp) * qty, 2)
        total_pnl += pnl_rs

        positions.append({
            "symbol": sym, "basket": basket,
            "entry_price": round(entry_price, 2),
            "ltp": round(ltp, 2),
            "qty": qty,
            "pnl": pnl_rs,
            "pnl_pct": pnl_pct,
            "occurrences": occurrences,
            "note": None,
        })

    return {
        "today_pnl": round(total_pnl, 2),
        "positions": positions,
        "market_open": market_open(),
        "shortlist_file": SHORTLIST_FILE,
        "ts": datetime.now(IST).isoformat(),
    }


# ───────────────────────────────────────────────
# REST endpoints
# ───────────────────────────────────────────────
@app.get("/api/shortlist")
def get_shortlist():
    df = load_shortlist()
    if df.empty:
        return {"error": f"{SHORTLIST_FILE} not found"}
    return df.to_dict(orient="records")

@app.get("/api/live/pnl")
def live_pnl_once():
    return compute_live_pnl()

@app.get("/api/debug/{symbol}")
def debug_symbol(symbol: str):
    """
    Diagnostic endpoint: shows the RAW yfinance response and the real
    underlying exception (if any) for one symbol, so you don't have to
    guess why a price is unavailable. Hit this in the browser, e.g.
    https://<your-backend>.onrender.com/api/debug/TCS
    """
    yf_sym = _yf_symbol(symbol)
    out = {"symbol": symbol, "yf_symbol": yf_sym}
    try:
        t = yf.Ticker(yf_sym)
        try:
            out["fast_info"] = dict(t.fast_info)
        except Exception as e:
            out["fast_info_error"] = repr(e)
        try:
            hist = t.history(period="1d", interval="1m")
            out["history_rows"] = len(hist)
            out["history_head"] = hist.head(3).reset_index().to_dict(orient="records") if not hist.empty else []
        except Exception as e:
            out["history_error"] = repr(e)
    except Exception as e:
        out["ticker_error"] = repr(e)
        out["traceback"] = traceback.format_exc()

    out["last_cached_error"] = _last_error.get(yf_sym)
    out["cached_open"] = _entry_cache.get(yf_sym)
    return out

@app.get("/api/intraday/{symbol}")
def intraday(symbol: str):
    """
    Today's 1-minute close-price series for a symbol, for sparkline charts.
    Returns {symbol, points: [{t, close}, ...]}
    """
    yf_sym = _yf_symbol(symbol)
    try:
        t = yf.Ticker(yf_sym)
        hist = t.history(period="1d", interval="1m")
        if hist.empty:
            return {"symbol": symbol, "points": [], "error": "no intraday data"}
        points = [
            {"t": idx.isoformat(), "close": round(float(row["Close"]), 2)}
            for idx, row in hist.iterrows()
        ]
        return {"symbol": symbol, "points": points}
    except Exception as e:
        return {"symbol": symbol, "points": [], "error": str(e)}

@app.get("/")
def health():
    return {
        "status": "ok",
        "time": datetime.now(IST).isoformat(),
        "market_open": market_open(),
        "data_source": "yfinance (no broker login required)",
        "shortlist_file": SHORTLIST_FILE,
        "capital_per_trade": CAPITAL_PER_TRADE,
    }


# ───────────────────────────────────────────────
# WebSocket — live feed every 5s
# ───────────────────────────────────────────────
clients: "set[WebSocket]" = set()

async def broadcaster():
    while True:
        if clients:
            payload = json.dumps(await asyncio.to_thread(compute_live_pnl))
            dead = []
            for ws in list(clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for d in dead:
                clients.discard(d)
        await asyncio.sleep(5)

@app.on_event("startup")
async def start_broadcaster():
    asyncio.create_task(broadcaster())

@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)
