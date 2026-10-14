import os
import pandas as pd
import numpy as np
import math
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import logging
from contextlib import asynccontextmanager
import httpx
from starlette.responses import Response

# ─────────────────────────────────────────────────────────────────────────────
# Indicators (Active in Backend, Hidden on Chart Canvas)
# ─────────────────────────────────────────────────────────────────────────────
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

CSV_FILENAME = "1h.csv"
NODE_URL = "http://127.0.0.1:10000"

DROP_FORMING_CANDLE = True

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: PEAK FORMATION + TRUE EXTERNAL CHoCH & BOS TUNING
# ─────────────────────────────────────────────────────────────────────────────
BASE_SWING_ORDER             = 4
FLIP_BUFFER_ATR              = 0.20
FLIP_CONFIRM_BARS            = 2
ANCHOR_MIN_MOVE_ATR          = 1.4
SWING_PROMINENCE_ATR         = 0.75

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}

CYCLE_STATE: Dict[str, Dict]   = {}
ORDER_CACHE: Dict[str, int]    = {}
ACTIVE_TRADES: Dict[str, Dict] = {}

base_dir = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        num = float(value)
        return default if (math.isnan(num) or math.isinf(num)) else num
    except Exception:
        return default


def load_csv_fallback():
    try:
        file_path = os.path.join(os.path.dirname(base_dir), CSV_FILENAME)
        if not os.path.exists(file_path):
            return None
        try:
            df = pd.read_csv(file_path, sep=';')
            if len(df.columns) < 2:
                df = pd.read_csv(file_path, sep=',')
        except Exception:
            df = pd.read_csv(file_path, sep=',')

        df.columns = [c.lower().strip() for c in df.columns]
        rename_map = {
            'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open',
            'date': 'Date', 'time': 'Date', 'timestamp': 'Date'
        }
        df.rename(columns=rename_map, inplace=True)

        numeric_cols = ['Open', 'High', 'Low', 'Close']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], format='mixed', dayfirst=False, errors='coerce')
            df.dropna(subset=['Date'], inplace=True)
            df['Date'] = (df['Date'] - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)

        df.dropna(subset=numeric_cols, inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df if len(df) > 50 else None
    except Exception as e:
        logger.error(f"CSV load failed: {e}")
        return None


def process_live_candles(candles_data: List[Dict]):
    try:
        df = pd.DataFrame(candles_data)
        rename_map = {
            'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open',
            'time': 'Date', 'timestamp': 'Date'
        }
        df.rename(columns=rename_map, inplace=True)
        for c in ['Open', 'High', 'Low', 'Close']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df.dropna(subset=['Open', 'High', 'Low', 'Close'], inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as e:
        logger.error(f"Live candle processing failed: {e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    MARKET_MEMORY["df"] = load_csv_fallback()
    yield
    MARKET_MEMORY["df"] = None


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalysisRequest(BaseModel):
    timeframe: str = "1h"
    currency: str = "XAUUSD"
    current_price: float = 0.0
    candles: Optional[List[Dict]] = None
    htf_candles: Optional[List[Dict]] = None
    news_data: Optional[Dict] = None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 ENGINE — Peak Formation + True External CHoCH & BOS
# ─────────────────────────────────────────────────────────────────────────────

def get_instrument_profile(currency: str, current_price: float) -> Dict:
    cu = currency.upper().replace("/", "").replace("-", "").replace("_", "")
    if cu in ("XAUUSD", "GOLD", "GC=F", "GCF"):
        return {"decimals": 2, "pip_size": 0.1, "label": "XAU/USD"}
    if cu in ("XAGUSD", "SILVER"):
        return {"decimals": 3, "pip_size": 0.001, "label": "XAG/USD"}
    if "JPY" in cu:
        return {"decimals": 3, "pip_size": 0.01, "label": currency}
    if any(cu.startswith(p) or cu.endswith(p) for p in ("EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "USD")):
        return {"decimals": 5, "pip_size": 0.0001, "label": currency}
    if current_price > 500:
        return {"decimals": 2, "pip_size": 0.1, "label": currency}
    if current_price > 20:
        return {"decimals": 3, "pip_size": 0.01, "label": currency}
    return {"decimals": 5, "pip_size": 0.0001, "label": currency}


def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    high  = df['High'].values
    low   = df['Low'].values
    close = df['Close'].values
    if len(close) < 2:
        return 0.0
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]))
    )
    if len(tr) < period:
        return float(np.mean(tr))
    atr_series = pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean()
    return float(atr_series.iloc[-1])


def adaptive_swing_order(df: pd.DataFrame, atr: float) -> int:
    if len(df) < 30 or atr <= 0:
        return BASE_SWING_ORDER
    recent_closes = df['Close'].tail(20).values
    net_move = abs(recent_closes[-1] - recent_closes[0])
    path_length = np.sum(np.abs(np.diff(recent_closes)))
    efficiency = net_move / path_length if path_length > 0 else 0.5
    order = int(np.clip(round(6.0 - (efficiency * 3.0)), 3, 6))
    return order


def detect_swings_causal(highs: np.ndarray, lows: np.ndarray, swing_order: int, min_prominence: float):
    n = len(highs)
    left_w = swing_order * 2
    right_w = swing_order
    filtered_highs = []
    filtered_lows = []

    for idx in range(left_w, n - right_w):
        l_highs = highs[idx - left_w : idx]
        r_highs = highs[idx + 1 : idx + right_w + 1]
        if highs[idx] >= np.max(l_highs) and highs[idx] > np.max(r_highs):
            left_prom  = highs[idx] - np.min(lows[idx - left_w : idx])
            right_prom = highs[idx] - np.min(lows[idx + 1 : idx + right_w + 1])
            if max(left_prom, right_prom) >= min_prominence and min(left_prom, right_prom) >= min_prominence * 0.40:
                if not filtered_highs or (idx - filtered_highs[-1]) >= right_w:
                    filtered_highs.append(idx)
                elif highs[idx] > highs[filtered_highs[-1]]:
                    filtered_highs[-1] = idx

        l_lows = lows[idx - left_w : idx]
        r_lows = lows[idx + 1 : idx + right_w + 1]
        if lows[idx] <= np.min(l_lows) and lows[idx] < np.min(r_lows):
            left_prom  = np.max(highs[idx - left_w : idx]) - lows[idx]
            right_prom = np.max(highs[idx + 1 : idx + right_w + 1]) - lows[idx]
            if max(left_prom, right_prom) >= min_prominence and min(left_prom, right_prom) >= min_prominence * 0.40:
                if not filtered_lows or (idx - filtered_lows[-1]) >= right_w:
                    filtered_lows.append(idx)
                elif lows[idx] < lows[filtered_lows[-1]]:
                    filtered_lows[-1] = idx

    return np.array(filtered_highs, dtype=int), np.array(filtered_lows, dtype=int)


def find_choch_and_bos_from_peak(df: pd.DataFrame, is_bullish: bool, anchor_idx: int,
                                 raw_highs: np.ndarray, raw_lows: np.ndarray,
                                 atr: float, decimals: int) -> List[Dict]:
    """
    VIDEO STRATEGY STEP 1 (14:10 - 20:10):
    1. TRUE CHoCH:
       - In a Bullish Trend (from Peak Formation Low): CHoCH is the break of the
         LAST LOWER HIGH (the origin Swing High that sent price down into the Peak Low).
       - In a Bearish Trend (from Peak Formation High): CHoCH is the break of the
         LAST HIGHER LOW (the origin Swing Low that sent price up into the Peak High).
    2. TRUE EXTERNAL BOS CHAIN:
       - After each break (CHoCH or BOS) at bar `break_idx`, we track the new impulse
         extreme until price makes a real pullback (`>= 0.65 * ATR`), and when a subsequent
         candle closes beyond that impulse extreme, it is marked as `BOS`.
    """
    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(closes)

    anchor_idx = int(np.clip(anchor_idx, 0, total - 1))
    buf        = 0.08 * atr
    events     = []

    if is_bullish:
        # ── 1. FIND THE LAST LOWER HIGH (PRE-PEAK SWING HIGH) THAT CAUSED THE PEAK LOW ──
        peak_low_val = float(lows[anchor_idx])
        search_left  = max(0, anchor_idx - 35)

        pre_high_candidates = [
            int(i) for i in raw_highs
            if search_left <= int(i) < anchor_idx
            and (highs[int(i)] - peak_low_val) >= (1.10 * atr)
        ]

        if not pre_high_candidates:
            for j in range(anchor_idx - 1, search_left, -1):
                l_w = max(0, j - 3)
                r_w = min(anchor_idx + 1, j + 4)
                if highs[j] >= np.max(highs[l_w:r_w]) and (highs[j] - peak_low_val) >= (1.10 * atr):
                    pre_high_candidates.append(j)
                    break

        if not pre_high_candidates and anchor_idx > 0:
            win_start = max(0, anchor_idx - 20)
            pre_high_candidates.append(win_start + int(np.argmax(highs[win_start:anchor_idx])))

        choch_swing_idx = max(pre_high_candidates) if pre_high_candidates else anchor_idx
        nb_lo = max(0, choch_swing_idx - 2)
        nb_hi = min(anchor_idx, choch_swing_idx + 3)
        if nb_hi > nb_lo:
            choch_swing_idx = nb_lo + int(np.argmax(highs[nb_lo:nb_hi]))

        choch_lvl = float(highs[choch_swing_idx])

        choch_break_idx = None
        for m in range(anchor_idx + 1, total):
            if closes[m] > choch_lvl + buf:
                choch_break_idx = m
                events.append({
                    "kind":       "CHoCH",
                    "level":      round(choch_lvl, decimals),
                    "swing_idx":  int(choch_swing_idx),
                    "start_time": int(dates[choch_swing_idx]),
                    "break_idx":  int(m),
                    "end_time":   int(dates[m]),
                })
                break

        if choch_break_idx is None:
            post_highs = [int(i) for i in raw_highs if anchor_idx < int(i) < total - 1 and (highs[int(i)] - peak_low_val) >= 1.20 * atr]
            for sh_idx in post_highs:
                sh_val = float(highs[sh_idx])
                for m in range(sh_idx + 1, total):
                    if closes[m] > sh_val + buf:
                        choch_break_idx = m
                        choch_lvl       = sh_val
                        events.append({
                            "kind":       "CHoCH",
                            "level":      round(sh_val, decimals),
                            "swing_idx":  int(sh_idx),
                            "start_time": int(dates[sh_idx]),
                            "break_idx":  int(m),
                            "end_time":   int(dates[m]),
                        })
                        break
                if choch_break_idx is not None:
                    break

        # ── 2. WALK FORWARD TO FIND EVERY SUBSEQUENT EXTERNAL BULLISH BOS ───
        if choch_break_idx is not None:
            last_broken_high = choch_lvl
            leg_start        = choch_break_idx

            while leg_start < total - 2:
                # Track the running impulse high starting from leg_start until a real pullback (>= 0.60 * ATR) occurs
                run_high_idx   = leg_start
                run_high_val   = float(highs[leg_start])
                pullback_ready = False
                found_bos      = False

                for m in range(leg_start + 1, total):
                    if not pullback_ready:
                        if highs[m] >= run_high_val:
                            run_high_val = float(highs[m])
                            run_high_idx = m
                        elif (run_high_val - lows[m]) >= (0.60 * atr) and run_high_val > last_broken_high + (0.12 * atr):
                            pullback_ready = True

                    if pullback_ready:
                        # If a candle closes above run_high_val, that confirms an external Bullish BOS!
                        if closes[m] > run_high_val + buf:
                            events.append({
                                "kind":       "BOS",
                                "level":      round(run_high_val, decimals),
                                "swing_idx":  int(run_high_idx),
                                "start_time": int(dates[run_high_idx]),
                                "break_idx":  int(m),
                                "end_time":   int(dates[m]),
                            })
                            last_broken_high = run_high_val
                            leg_start        = m
                            found_bos        = True
                            break
                        # If a wick pokes slightly above run_high_val without closing above it yet, update the swing high
                        elif highs[m] > run_high_val:
                            run_high_val   = float(highs[m])
                            run_high_idx   = m
                            pullback_ready = False

                if not found_bos:
                    break

    else:
        # ── 1. FIND THE LAST HIGHER LOW (PRE-PEAK SWING LOW) THAT CAUSED THE PEAK HIGH ──
        peak_high_val = float(highs[anchor_idx])
        search_left   = max(0, anchor_idx - 35)

        pre_low_candidates = [
            int(i) for i in raw_lows
            if search_left <= int(i) < anchor_idx
            and (peak_high_val - lows[int(i)]) >= (1.10 * atr)
        ]

        if not pre_low_candidates:
            for j in range(anchor_idx - 1, search_left, -1):
                l_w = max(0, j - 3)
                r_w = min(anchor_idx + 1, j + 4)
                if lows[j] <= np.min(lows[l_w:r_w]) and (peak_high_val - lows[j]) >= (1.10 * atr):
                    pre_low_candidates.append(j)
                    break

        if not pre_low_candidates and anchor_idx > 0:
            win_start = max(0, anchor_idx - 20)
            pre_low_candidates.append(win_start + int(np.argmin(lows[win_start:anchor_idx])))

        choch_swing_idx = max(pre_low_candidates) if pre_low_candidates else anchor_idx
        nb_lo = max(0, choch_swing_idx - 2)
        nb_hi = min(anchor_idx, choch_swing_idx + 3)
        if nb_hi > nb_lo:
            choch_swing_idx = nb_lo + int(np.argmin(lows[nb_lo:nb_hi]))

        choch_lvl = float(lows[choch_swing_idx])

        choch_break_idx = None
        for m in range(anchor_idx + 1, total):
            if closes[m] < choch_lvl - buf:
                choch_break_idx = m
                events.append({
                    "kind":       "CHoCH",
                    "level":      round(choch_lvl, decimals),
                    "swing_idx":  int(choch_swing_idx),
                    "start_time": int(dates[choch_swing_idx]),
                    "break_idx":  int(m),
                    "end_time":   int(dates[m]),
                })
                break

        if choch_break_idx is None:
            post_lows = [int(i) for i in raw_lows if anchor_idx < int(i) < total - 1 and (peak_high_val - lows[int(i)]) >= 1.20 * atr]
            for sl_idx in post_lows:
                sl_val = float(lows[sl_idx])
                for m in range(sl_idx + 1, total):
                    if closes[m] < sl_val - buf:
                        choch_break_idx = m
                        choch_lvl       = sl_val
                        events.append({
                            "kind":       "CHoCH",
                            "level":      round(sl_val, decimals),
                            "swing_idx":  int(sl_idx),
                            "start_time": int(dates[sl_idx]),
                            "break_idx":  int(m),
                            "end_time":   int(dates[m]),
                        })
                        break
                if choch_break_idx is not None:
                    break

        # ── 2. WALK FORWARD TO FIND EVERY SUBSEQUENT EXTERNAL BEARISH BOS ───
        if choch_break_idx is not None:
            last_broken_low = choch_lvl
            leg_start       = choch_break_idx

            while leg_start < total - 2:
                run_low_idx    = leg_start
                run_low_val    = float(lows[leg_start])
                pullback_ready = False
                found_bos      = False

                for m in range(leg_start + 1, total):
                    if not pullback_ready:
                        if lows[m] <= run_low_val:
                            run_low_val = float(lows[m])
                            run_low_idx = m
                        elif (highs[m] - run_low_val) >= (0.60 * atr) and run_low_val < last_broken_low - (0.12 * atr):
                            pullback_ready = True

                    if pullback_ready:
                        if closes[m] < run_low_val - buf:
                            events.append({
                                "kind":       "BOS",
                                "level":      round(run_low_val, decimals),
                                "swing_idx":  int(run_low_idx),
                                "start_time": int(dates[run_low_idx]),
                                "break_idx":  int(m),
                                "end_time":   int(dates[m]),
                            })
                            last_broken_low = run_low_val
                            leg_start       = m
                            found_bos       = True
                            break
                        elif lows[m] < run_low_val:
                            run_low_val    = float(lows[m])
                            run_low_idx    = m
                            pullback_ready = False

                if not found_bos:
                    break

    return events


def analyze_market_structure(df: pd.DataFrame, profile: Dict,
                             current_price: float = 0.0,
                             swing_order_override: Optional[int] = None,
                             state_key: Optional[str] = None,
                             drop_forming: bool = False) -> Dict:
    highs  = df['High'].values.copy()
    lows   = df['Low'].values.copy()
    closes = df['Close'].values.copy()
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(closes)
    decimals = profile.get('decimals', 5)

    if current_price > 0 and total > 0:
        closes[-1] = current_price
        highs[-1]  = max(highs[-1], current_price)
        lows[-1]   = min(lows[-1], current_price)

    atr = calculate_atr(df, 14)
    if atr == 0:
        atr = float(df['Close'].mean()) * 0.001

    fresh_order = adaptive_swing_order(df, atr)
    if swing_order_override is not None:
        swing_order = swing_order_override
    elif state_key:
        cached = ORDER_CACHE.get(state_key)
        if cached is None or abs(fresh_order - cached) >= 3:
            ORDER_CACHE[state_key] = fresh_order
            cached = fresh_order
        swing_order = cached
    else:
        swing_order = fresh_order

    min_prominence = atr * SWING_PROMINENCE_ATR
    raw_highs, raw_lows = detect_swings_causal(highs, lows, swing_order, min_prominence)

    ema_50_array  = df['Close'].ewm(span=50, adjust=False).mean().values
    ema_200_array = df['Close'].ewm(span=200, adjust=False).mean().values

    # ── 1. Protected Swing Cycle Engine (Locks True Peak Formation High/Low) ──
    def compute_structural_cycles(seed_bullish: bool):
        highs_set = set(raw_highs.tolist())
        lows_set  = set(raw_lows.tolist())

        is_bull = seed_bullish
        cycle_start_idx = 0
        extreme_price = highs[0] if is_bull else lows[0]

        protected_low_idx   = 0
        protected_high_idx  = 0
        last_confirmed_low  = None
        last_confirmed_high = None

        buf = FLIP_BUFFER_ATR * atr
        pending = 0
        events = []

        for i in range(total):
            confirmed_bar = i - swing_order
            if confirmed_bar in highs_set:
                last_confirmed_high = confirmed_bar
            if confirmed_bar in lows_set:
                last_confirmed_low = confirmed_bar

            if is_bull:
                if highs[i] >= extreme_price:
                    extreme_price = highs[i]
                    if last_confirmed_low is not None and last_confirmed_low >= cycle_start_idx:
                        protected_low_idx = last_confirmed_low

                if protected_low_idx == cycle_start_idx and last_confirmed_low is not None and last_confirmed_low > cycle_start_idx:
                    if highs[i] - lows[last_confirmed_low] >= ANCHOR_MIN_MOVE_ATR * atr:
                        protected_low_idx = last_confirmed_low

                struct_broke = closes[i] < (lows[protected_low_idx] - buf)
                ema_crash = (
                    closes[i] < min(ema_50_array[i], ema_200_array[i]) - (1.2 * atr)
                    and (extreme_price - closes[i]) >= (2.8 * atr)
                )
                broke = struct_broke or ema_crash
                pending = pending + 1 if broke else 0

                if pending >= FLIP_CONFIRM_BARS or (i == total - 1 and broke and (extreme_price - closes[i]) >= 2.2 * atr):
                    true_peak_high_idx = cycle_start_idx + int(np.argmax(highs[cycle_start_idx : i + 1]))
                    is_bull            = False
                    cycle_start_idx    = true_peak_high_idx
                    extreme_price      = lows[i]
                    protected_high_idx = true_peak_high_idx
                    pending            = 0
                    events.append({
                        "flip_idx":     i,
                        "flip_time":    int(dates[i]),
                        "bullish":      False,
                        "anchor_idx":   true_peak_high_idx,
                        "anchor_time":  int(dates[true_peak_high_idx]),
                        "anchor_price": float(highs[true_peak_high_idx]),
                    })
            else:
                if lows[i] <= extreme_price:
                    extreme_price = lows[i]
                    if last_confirmed_high is not None and last_confirmed_high >= cycle_start_idx:
                        protected_high_idx = last_confirmed_high

                if protected_high_idx == cycle_start_idx and last_confirmed_high is not None and last_confirmed_high > cycle_start_idx:
                    if highs[last_confirmed_high] - lows[i] >= ANCHOR_MIN_MOVE_ATR * atr:
                        protected_high_idx = last_confirmed_high

                struct_broke = closes[i] > (highs[protected_high_idx] + buf)
                ema_surge = (
                    closes[i] > max(ema_50_array[i], ema_200_array[i]) + (1.2 * atr)
                    and (closes[i] - extreme_price) >= (2.8 * atr)
                )
                broke = struct_broke or ema_surge
                pending = pending + 1 if broke else 0

                if pending >= FLIP_CONFIRM_BARS or (i == total - 1 and broke and (closes[i] - extreme_price) >= 2.2 * atr):
                    true_peak_low_idx = cycle_start_idx + int(np.argmin(lows[cycle_start_idx : i + 1]))
                    is_bull           = True
                    cycle_start_idx   = true_peak_low_idx
                    extreme_price     = highs[i]
                    protected_low_idx = true_peak_low_idx
                    pending           = 0
                    events.append({
                        "flip_idx":     i,
                        "flip_time":    int(dates[i]),
                        "bullish":      True,
                        "anchor_idx":   true_peak_low_idx,
                        "anchor_time":  int(dates[true_peak_low_idx]),
                        "anchor_price": float(lows[true_peak_low_idx]),
                    })

        return events, is_bull

    seed_idx = min(50, total - 1)
    seed_bullish = bool(ema_50_array[seed_idx] > ema_200_array[seed_idx])
    cycle_events, is_bullish = compute_structural_cycles(seed_bullish)

    matching_events = [e for e in cycle_events if e["bullish"] == is_bullish]
    if matching_events:
        latest_event       = matching_events[-1]
        fresh_anchor_idx   = latest_event["anchor_idx"]
        fresh_anchor_time  = latest_event["anchor_time"]
        fresh_anchor_price = latest_event["anchor_price"]
    else:
        fresh_anchor_idx   = int(np.argmin(lows)) if is_bullish else int(np.argmax(highs))
        fresh_anchor_time  = int(dates[fresh_anchor_idx])
        fresh_anchor_price = float(lows[fresh_anchor_idx]) if is_bullish else float(highs[fresh_anchor_idx])

    current_idx = total - 1
    if (current_idx - fresh_anchor_idx) > 120:
        dist_atr = abs(closes[-1] - fresh_anchor_price) / atr if atr > 0 else 0
        if dist_atr > 5.5:
            recent_start = max(0, current_idx - 110)
            if is_bullish:
                cand_lows = [
                    idx for idx in raw_lows
                    if recent_start <= idx <= current_idx - swing_order
                    and (highs[idx:].max() - lows[idx]) >= (ANCHOR_MIN_MOVE_ATR * atr)
                    and closes[-1] > lows[idx] + (1.0 * atr)
                ]
                if cand_lows:
                    new_idx = min(cand_lows, key=lambda idx: lows[idx])
                    fresh_anchor_idx   = int(new_idx)
                    fresh_anchor_time  = int(dates[fresh_anchor_idx])
                    fresh_anchor_price = float(lows[fresh_anchor_idx])
            else:
                cand_highs = [
                    idx for idx in raw_highs
                    if recent_start <= idx <= current_idx - swing_order
                    and (highs[idx] - lows[idx:].min()) >= (ANCHOR_MIN_MOVE_ATR * atr)
                    and closes[-1] < highs[idx] - (1.0 * atr)
                ]
                if cand_highs:
                    new_idx = max(cand_highs, key=lambda idx: highs[idx])
                    fresh_anchor_idx   = int(new_idx)
                    fresh_anchor_time  = int(dates[fresh_anchor_idx])
                    fresh_anchor_price = float(highs[fresh_anchor_idx])

    anchor_idx   = fresh_anchor_idx
    anchor_time  = fresh_anchor_time
    anchor_price = fresh_anchor_price

    if is_bullish:
        post_min_idx = anchor_idx + int(np.argmin(lows[anchor_idx:]))
        if lows[post_min_idx] < anchor_price:
            anchor_idx   = post_min_idx
            anchor_time  = int(dates[anchor_idx])
            anchor_price = float(lows[anchor_idx])
        move = float(highs[anchor_idx:].max() - anchor_price)
        extreme_held = bool(lows[anchor_idx:].min() >= anchor_price - 1e-9)
    else:
        post_max_idx = anchor_idx + int(np.argmax(highs[anchor_idx:]))
        if highs[post_max_idx] > anchor_price:
            anchor_idx   = post_max_idx
            anchor_time  = int(dates[anchor_idx])
            anchor_price = float(highs[anchor_idx])
        move = float(anchor_price - lows[anchor_idx:].min())
        extreme_held = bool(highs[anchor_idx:].max() <= anchor_price + 1e-9)

    peak_confirmed = bool(extreme_held and move >= ANCHOR_MIN_MOVE_ATR * atr)

    # ── 2. Find Exact CHoCH and Subsequent External BOS Breaks From the Peak ──
    struct_events = find_choch_and_bos_from_peak(
        df, is_bullish, anchor_idx, raw_highs, raw_lows, atr, decimals
    )

    if not is_bullish:
        pattern_name = f"Peak Formation High ({anchor_price:.{decimals}f})"
        anchor_color = "rgba(255, 59, 59, 0.95)"
    else:
        pattern_name = f"Peak Formation Low ({anchor_price:.{decimals}f})"
        anchor_color = "rgba(59, 255, 130, 0.95)"

    # ── 3. Build Step 1 Lines ONLY (Peak Line + True CHoCH + External BOS) ──
    all_lines = [{
        "level":      round(anchor_price, decimals),
        "start_time": anchor_time,
        "end_time":   int(dates[-1]),
        "type":       pattern_name,
        "color":      anchor_color,
        "is_anchor":  True,
        "is_struct":  False,
    }]

    for ev in struct_events:
        arrow = "▲" if is_bullish else "▼"
        is_c  = (ev["kind"] == "CHoCH")
        col   = (
            "rgba(0, 230, 118, 0.95)" if (is_bullish and is_c)
            else "rgba(38, 166, 154, 0.95)" if is_bullish
            else "rgba(255, 82, 82, 0.95)" if is_c
            else "rgba(239, 83, 80, 0.95)"
        )
        all_lines.append({
            "level":      ev["level"],
            "start_time": ev["start_time"],
            "end_time":   ev["end_time"],
            "type":       f"{ev['kind']} {arrow} ({ev['level']:.{decimals}f})",
            "color":      col,
            "is_anchor":  False,
            "is_struct":  True,
            "is_choch":   is_c,
        })

    if struct_events:
        latest_ev   = struct_events[-1]
        break_kind  = latest_ev["kind"]
        break_level = latest_ev["level"]
    else:
        break_kind  = "CHoCH"
        break_level = round(anchor_price, decimals)

    if is_bullish:
        cycle = f"BULLISH TREND (Peak W + {break_kind} ▲)"
    else:
        cycle = f"BEARISH TREND (Peak M + {break_kind} ▼)"

    return {
        "cycle":           cycle,
        "is_bullish":      is_bullish,
        "break_kind":      break_kind,
        "break_level":     break_level,
        "struct_events":   struct_events,
        "lines":           all_lines,
        "anchor":          round(anchor_price, decimals),
        "anchor_idx":      int(anchor_idx),
        "atr":             atr,
        "peak_confirmed":  peak_confirmed,
        "sweeps":          [],
        "swing_order":     swing_order,
        "raw_highs":       raw_highs.tolist(),
        "raw_lows":        raw_lows.tolist(),
        "trendline":       None,
        "zone_boxes":      [],
        "poi_level":       round(anchor_price, decimals),
        "targets":         [],
        "projection_path": [],
    }


def evaluate_entry_signal(df: pd.DataFrame, full_df: pd.DataFrame, ms: Dict, current_price: float,
                          ema_50: float, ema_200: float, rsi: float,
                          htf_aligned: bool, decimals: int, pip_size: float,
                          state_key: Optional[str] = None) -> Dict:
    is_bullish     = ms["is_bullish"]
    peak_confirmed = ms["peak_confirmed"]
    anchor_price   = ms["anchor"]
    break_kind     = ms["break_kind"]
    break_level    = ms["break_level"]
    struct_events  = ms["struct_events"]

    choch_ev  = next((e for e in struct_events if e["kind"] == "CHoCH"), None)
    bos_evs   = [e for e in struct_events if e["kind"] == "BOS"]
    dir_word  = "BULLISH (BUY BIAS)" if is_bullish else "BEARISH (SELL BIAS)"
    peak_word = "Peak Formation Low (W)" if is_bullish else "Peak Formation High (M)"
    arrow     = "▲" if is_bullish else "▼"

    checklist = [
        {
            "step":   f"1. {peak_word}",
            "done":   bool(peak_confirmed),
            "detail": f"{anchor_price:.{decimals}f} ✅" if peak_confirmed else f"{anchor_price:.{decimals}f}",
        },
        {
            "step":   f"2. Change of Character (CHoCH {arrow})",
            "done":   bool(choch_ev is not None),
            "detail": f"Cleared {choch_ev['level']:.{decimals}f} ✅" if choch_ev else "Waiting",
        },
        {
            "step":   f"3. Break of Structure (BOS {arrow})",
            "done":   bool(len(bos_evs) > 0),
            "detail": f"Cleared {bos_evs[-1]['level']:.{decimals}f} ({len(bos_evs)}x) ✅" if bos_evs else "Awaiting Next BOS",
        },
        {
            "step":   "4. Trend Direction Confirmed",
            "done":   bool(peak_confirmed),
            "detail": dir_word,
        },
    ]

    note = (
        f"📊 STEP 1 STRUCTURE: {peak_word} locked @ {anchor_price:.{decimals}f}. "
        f"Latest structure break: {break_kind} {arrow} @ {break_level:.{decimals}f} -> Market Direction is {dir_word}."
    )

    return {
        "signal":     "WAIT",
        "confidence": 60 if len(bos_evs) > 0 else 50,
        "setup_name": f"Step 1: {dir_word}",
        "checklist":  checklist,
        "execution_plan": {
            "stage":             "AWAITING_PULLBACK",
            "trigger_condition": note,
            "planned_entry":     round(anchor_price, decimals),
            "planned_sl":        None,
            "planned_tp1":       None,
            "planned_tp2":       None,
            "risk_reward":       "—",
        },
        "entry_note":   note,
        "trade_levels": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# API ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    if req.candles and len(req.candles) > 50:
        df = process_live_candles(req.candles)
        data_source = "LIVE_NODE_DATA"
    else:
        df = MARKET_MEMORY["df"]
        data_source = "CSV_FALLBACK"

    if df is None or df.empty:
        return {"signal": "HOLD", "confidence": 0, "reasoning": ["⏳ Waiting for data..."]}

    try:
        csv_last_price = safe_float(df['Close'].iloc[-1])
        current_price  = req.current_price if req.current_price > 0 else csv_last_price

        profile  = get_instrument_profile(req.currency, current_price)
        decimals = profile['decimals']
        pip_size = profile['pip_size']

        ema_200 = safe_float(EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1], current_price)
        ema_50  = safe_float(EMAIndicator(close=df['Close'], window=50).ema_indicator().iloc[-1], current_price)
        rsi     = safe_float(RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1], 50.0)

        state_key = f"{req.currency}:{req.timeframe}"
        ms = analyze_market_structure(
            df, profile,
            current_price=current_price,
            state_key=state_key,
            drop_forming=False,
        )
        cycle          = ms['cycle']
        lines          = ms['lines']
        peak_confirmed = ms.get('peak_confirmed', False)

        bias_str = 'BULLISH' if ms['is_bullish'] else 'BEARISH'

        htf_aligned  = False
        htf_bias_str = "NEUTRAL"
        if req.htf_candles and len(req.htf_candles) > 50:
            htf_df = process_live_candles(req.htf_candles)
            if htf_df is not None:
                htf_ema50    = safe_float(EMAIndicator(close=htf_df['Close'], window=50).ema_indicator().iloc[-1])
                htf_ema200   = safe_float(EMAIndicator(close=htf_df['Close'], window=200).ema_indicator().iloc[-1])
                htf_bias_str = "BULLISH" if htf_ema50 > htf_ema200 else "BEARISH"
                htf_aligned  = (htf_bias_str == bias_str)
        else:
            htf_aligned  = (ms['is_bullish'] and ema_50 > ema_200) or ((not ms['is_bullish']) and ema_50 < ema_200)
            htf_bias_str = bias_str if htf_aligned else "NEUTRAL"

        eval_df = df.iloc[:-1].reset_index(drop=True) if (DROP_FORMING_CANDLE and data_source == "LIVE_NODE_DATA" and len(df) > 60) else df
        entry_eval = evaluate_entry_signal(
            eval_df, df, ms, current_price, ema_50, ema_200, rsi, htf_aligned, decimals, pip_size, state_key=state_key
        )

        checklist = entry_eval.get("checklist", [])
        reasoning = [
            f"{'✅' if c['done'] else '⏳'} {c['step']} ({c['detail']})"
            for c in checklist
        ]
        reasoning.append(entry_eval["entry_note"])

        return {
            "signal":         entry_eval["signal"],
            "confidence":     entry_eval["confidence"],
            "trend":          cycle,
            "pattern":        entry_eval["setup_name"],
            "peak_confirmed": peak_confirmed,
            "checklist":      checklist,
            "executionPlan":  entry_eval["execution_plan"],
            "reasoning":      reasoning,
            "keyLevels": {
                "resistance": round(ms["anchor"], decimals) if not ms["is_bullish"] else round(ms["break_level"], decimals),
                "support":    round(ms["anchor"], decimals) if ms["is_bullish"] else round(ms["break_level"], decimals),
                "poi":        ms["poi_level"],
                "target1":    None,
                "target2":    None,
                "ema200":     round(ema_200, decimals),
                "ema50":      round(ema_50, decimals),
            },
            "visuals": {
                "bos_lines":       lines,
                "sweeps":          [],
                "trendline":       None,
                "order_blocks":    [],
                "targets":         [],
                "projection_path": [],
            },
            "mtf_confluence": [
                {"tf": "4H", "bias": htf_bias_str, "strength": 88 if htf_aligned else 50},
                {"tf": "1H", "bias": bias_str, "strength": 85 if peak_confirmed else 60},
            ],
            "tradeSetup":     None,
            "dataSource":     data_source,
        }

    except Exception as e:
        logger.error(f"Analysis Crash: {e}", exc_info=True)
        return {"signal": "ERROR", "confidence": 0, "reasoning": [f"Engine error: {str(e)}"]}


@app.post("/api/debug")
async def debug_analysis(req: AnalysisRequest):
    if req.candles and len(req.candles) > 50:
        df = process_live_candles(req.candles)
    else:
        df = MARKET_MEMORY["df"]

    if df is None or df.empty:
        return {"error": "No data available"}

    try:
        current_price = req.current_price if req.current_price > 0 else safe_float(df['Close'].iloc[-1])
        profile = get_instrument_profile(req.currency, current_price)
        ms = analyze_market_structure(df, profile, current_price=current_price)
        return {
            "✅ ENGINE VERSION": "AuraBrain v8.2 — Step 1 (Complete External BOS Chain)",
            "📊 INSTRUMENT":     req.currency,
            "🎯 STRUCTURE":      ms['cycle'],
            "⚓ PEAK ANCHOR":    ms['anchor'],
            "📏 LINES":          ms['lines'],
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_to_node(path: str, request: Request):
    try:
        url = f"{NODE_URL}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        body = await request.body() if request.method in ["POST", "PUT"] else None
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=request.method,
                url=url,
                headers={k: v for k, v in request.headers.items()
                         if k.lower() not in ['host', 'content-length']},
                content=body
            )
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers)
        )
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        raise HTTPException(status_code=502, detail="Node backend unreachable")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)