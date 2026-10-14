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
# Indicators
# ─────────────────────────────────────────────────────────────────────────────
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

CSV_FILENAME = "1h.csv"
NODE_URL = "http://127.0.0.1:10000"

# If True, the last live candle is treated as still forming and ignored, so a
# break or entry signal only counts once a candle has actually CLOSED.
DROP_FORMING_CANDLE = True

# ─────────────────────────────────────────────────────────────────────────────
# PEAK CONFIRMATION & ENTRY TUNING
# ─────────────────────────────────────────────────────────────────────────────
BASE_SWING_ORDER             = 4
FLIP_BUFFER_ATR              = 0.25
FLIP_CONFIRM_BARS            = 2
ANCHOR_MIN_MOVE_ATR          = 1.5
SWING_PROMINENCE_ATR         = 0.90
DOUBLE_PATTERN_TOLERANCE_ATR = 0.65

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}

# Anti-flicker & Anchor-Lock memory (per "currency:timeframe"):
CYCLE_STATE: Dict[str, Dict] = {}
ORDER_CACHE: Dict[str, int] = {}

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
# CORE ENGINE — Peak Formation, Structural Targets & Visual Roadmap
# ─────────────────────────────────────────────────────────────────────────────

def get_instrument_profile(currency: str, current_price: float) -> Dict:
    cu = currency.upper().replace("/", "").replace("-", "").replace("_", "")
    if cu in ("XAUUSD", "GOLD"):
        return {"decimals": 2, "pip_size": 0.01, "label": "XAU/USD"}
    if cu in ("XAGUSD", "SILVER"):
        return {"decimals": 3, "pip_size": 0.001, "label": "XAG/USD"}
    if "JPY" in cu:
        return {"decimals": 3, "pip_size": 0.01, "label": currency}
    if any(cu.startswith(p) or cu.endswith(p) for p in ("EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "USD")):
        return {"decimals": 5, "pip_size": 0.0001, "label": currency}
    if current_price > 5000:
        return {"decimals": 1, "pip_size": 0.1, "label": currency}
    if current_price > 100:
        return {"decimals": 2, "pip_size": 0.01, "label": currency}
    return {"decimals": 5, "pip_size": 0.0001, "label": currency}


def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Wilder's smoothed ATR (matches TradingView / MT4 / MT5)."""
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
            if max(left_prom, right_prom) >= min_prominence and min(left_prom, right_prom) >= min_prominence * 0.45:
                if not filtered_highs or (idx - filtered_highs[-1]) >= right_w:
                    filtered_highs.append(idx)
                elif highs[idx] > highs[filtered_highs[-1]]:
                    filtered_highs[-1] = idx

        l_lows = lows[idx - left_w : idx]
        r_lows = lows[idx + 1 : idx + right_w + 1]
        if lows[idx] <= np.min(l_lows) and lows[idx] < np.min(r_lows):
            left_prom  = np.max(highs[idx - left_w : idx]) - lows[idx]
            right_prom = np.max(highs[idx + 1 : idx + right_w + 1]) - lows[idx]
            if max(left_prom, right_prom) >= min_prominence and min(left_prom, right_prom) >= min_prominence * 0.45:
                if not filtered_lows or (idx - filtered_lows[-1]) >= right_w:
                    filtered_lows.append(idx)
                elif lows[idx] < lows[filtered_lows[-1]]:
                    filtered_lows[-1] = idx

    return np.array(filtered_highs, dtype=int), np.array(filtered_lows, dtype=int)


def find_structural_targets(df: pd.DataFrame, raw_highs: np.ndarray, raw_lows: np.ndarray,
                            is_bullish: bool, current_price: float, anchor_price: float,
                            atr: float, decimals: int) -> List[Dict]:
    """
    Finds the 2 primary opposing structural liquidity targets (Target 1 & Target 2)
    from prior major swing highs (in a Bullish cycle) or prior major swing lows
    (in a Bearish cycle), matching the institutional chart roadmap.
    """
    highs = df['High'].values
    lows  = df['Low'].values
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    end_time = int(dates[-1])

    targets = []
    min_dist = 0.85 * atr        # Target 1 must be at least 0.85 ATR away from current price
    min_separation = 1.10 * atr  # Target 2 must be materially beyond Target 1

    if is_bullish:
        # Look for historical swing highs above current price
        candidates = []
        for idx in raw_highs:
            lvl = float(highs[idx])
            if lvl >= current_price + min_dist:
                candidates.append((lvl, int(dates[idx])))
        # Sort from nearest above price to highest
        candidates.sort(key=lambda x: x[0])

        for lvl, t_start in candidates:
            if not targets or (lvl - targets[-1]["level"]) >= min_separation:
                t_num = len(targets) + 1
                targets.append({
                    "target_num": t_num,
                    "level":      round(lvl, decimals),
                    "top":        round(lvl + (0.12 * atr), decimals),
                    "bottom":     round(lvl - (0.12 * atr), decimals),
                    "start_time": t_start,
                    "end_time":   end_time,
                    "label":      f"Target {t_num} ({lvl:.{decimals}f})",
                })
                if len(targets) >= 2:
                    break

        # Fallback if price is at all-time chart highs
        while len(targets) < 2:
            t_num = len(targets) + 1
            base_lvl = targets[-1]["level"] if targets else max(current_price, anchor_price + 1.5 * atr)
            proj_lvl = round(base_lvl + (1.8 * atr), decimals)
            targets.append({
                "target_num": t_num,
                "level":      proj_lvl,
                "top":        round(proj_lvl + (0.12 * atr), decimals),
                "bottom":     round(proj_lvl - (0.12 * atr), decimals),
                "start_time": int(dates[max(0, len(dates) - 40)]),
                "end_time":   end_time,
                "label":      f"Target {t_num} ({proj_lvl:.{decimals}f})",
            })

    else:
        # Bearish Cycle: Look for historical swing lows below current price
        candidates = []
        for idx in raw_lows:
            lvl = float(lows[idx])
            if lvl <= current_price - min_dist:
                candidates.append((lvl, int(dates[idx])))
        # Sort from nearest below price to lowest
        candidates.sort(key=lambda x: x[0], reverse=True)

        for lvl, t_start in candidates:
            if not targets or (targets[-1]["level"] - lvl) >= min_separation:
                t_num = len(targets) + 1
                targets.append({
                    "target_num": t_num,
                    "level":      round(lvl, decimals),
                    "top":        round(lvl + (0.12 * atr), decimals),
                    "bottom":     round(lvl - (0.12 * atr), decimals),
                    "start_time": t_start,
                    "end_time":   end_time,
                    "label":      f"Target {t_num} ({lvl:.{decimals}f})",
                })
                if len(targets) >= 2:
                    break

        while len(targets) < 2:
            t_num = len(targets) + 1
            base_lvl = targets[-1]["level"] if targets else min(current_price, anchor_price - 1.5 * atr)
            proj_lvl = round(base_lvl - (1.8 * atr), decimals)
            targets.append({
                "target_num": t_num,
                "level":      proj_lvl,
                "top":        round(proj_lvl + (0.12 * atr), decimals),
                "bottom":     round(proj_lvl - (0.12 * atr), decimals),
                "start_time": int(dates[max(0, len(dates) - 40)]),
                "end_time":   end_time,
                "label":      f"Target {t_num} ({proj_lvl:.{decimals}f})",
            })

    return targets


def detect_liquidity_sweeps(df: pd.DataFrame, swing_highs: np.ndarray, swing_lows: np.ndarray, atr: float) -> List[Dict]:
    sweeps = []
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    min_wick = atr * 0.20
    total    = len(closes)
    cutoff   = max(0, total - 200)

    recent_highs = swing_highs[swing_highs >= cutoff]
    recent_lows  = swing_lows[swing_lows >= cutoff]

    for sh_idx in recent_highs:
        sh_price = highs[sh_idx]
        for i in range(sh_idx + 1, min(sh_idx + 80, total)):
            wick_above = highs[i] - sh_price
            if closes[i] > sh_price + (atr * 0.25):
                break
            if wick_above >= min_wick and closes[i] < sh_price:
                sweeps.append({
                    "type":      "BEAR_SWEEP",
                    "level":     float(sh_price),
                    "sweep_idx": int(i),
                    "time":      int(dates[i]) if len(dates) > i else 0,
                    "wick_size": float(wick_above),
                })
                break

    for sl_idx in recent_lows:
        sl_price = lows[sl_idx]
        for i in range(sl_idx + 1, min(sl_idx + 80, total)):
            wick_below = sl_price - lows[i]
            if closes[i] < sl_price - (atr * 0.25):
                break
            if wick_below >= min_wick and closes[i] > sl_price:
                sweeps.append({
                    "type":      "BULL_SWEEP",
                    "level":     float(sl_price),
                    "sweep_idx": int(i),
                    "time":      int(dates[i]) if len(dates) > i else 0,
                    "wick_size": float(wick_below),
                })
                break
    sweeps.sort(key=lambda x: x['sweep_idx'])
    return sweeps[-5:]


def detect_double_pattern(df: pd.DataFrame, swing_highs: np.ndarray, swing_lows: np.ndarray,
                          atr: float, anchor_idx: Optional[int] = None, lookback: int = 150) -> Dict:
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    total  = len(closes)
    cutoff = max(0, total - lookback)

    price_tolerance   = atr * DOUBLE_PATTERN_TOLERANCE_ATR
    middle_prominence = atr * 0.45

    result = {
        "w_confirmed": False, "w_low1": None, "w_low2": None, "w_middle": None, "w_low2_idx": None,
        "m_confirmed": False, "m_high1": None, "m_high2": None, "m_middle": None, "m_high2_idx": None,
    }

    recent_lows  = sorted([int(i) for i in swing_lows if i >= cutoff])
    recent_highs = sorted([int(i) for i in swing_highs if i >= cutoff])

    for k in range(len(recent_lows) - 1, 0, -1):
        low2_idx = recent_lows[k]
        low1_idx = recent_lows[k - 1]
        if (low2_idx - low1_idx) > 65:
            continue
        if anchor_idx is not None and abs(low1_idx - anchor_idx) > 25 and abs(low2_idx - anchor_idx) > 25 and k < len(recent_lows) - 1:
            continue

        low1_price = lows[low1_idx]
        low2_price = lows[low2_idx]

        if abs(low1_price - low2_price) <= price_tolerance:
            between_highs = [h for h in range(low1_idx + 1, low2_idx)]
            if between_highs:
                middle_idx   = max(between_highs, key=lambda h: highs[h])
                middle_price = highs[middle_idx]
                if middle_price - min(low1_price, low2_price) >= middle_prominence:
                    post_closes = closes[low2_idx + 1:]
                    post_lows   = lows[low2_idx + 1:]
                    if len(post_closes) > 0 and np.max(post_closes) > middle_price and np.min(post_lows) >= min(low1_price, low2_price) - (0.1 * atr):
                        result.update({
                            "w_confirmed": True,
                            "w_low1": float(low1_price),
                            "w_low2": float(low2_price),
                            "w_middle": float(middle_price),
                            "w_low2_idx": int(low2_idx),
                        })
                        break

    for k in range(len(recent_highs) - 1, 0, -1):
        high2_idx = recent_highs[k]
        high1_idx = recent_highs[k - 1]
        if (high2_idx - high1_idx) > 65:
            continue
        if anchor_idx is not None and abs(high1_idx - anchor_idx) > 25 and abs(high2_idx - anchor_idx) > 25 and k < len(recent_highs) - 1:
            continue

        high1_price = highs[high1_idx]
        high2_price = highs[high2_idx]

        if abs(high1_price - high2_price) <= price_tolerance:
            between_lows = [l for l in range(high1_idx + 1, high2_idx)]
            if between_lows:
                middle_idx   = min(between_lows, key=lambda l: lows[l])
                middle_price = lows[middle_idx]
                if max(high1_price, high2_price) - middle_price >= middle_prominence:
                    post_closes = closes[high2_idx + 1:]
                    post_highs  = highs[high2_idx + 1:]
                    if len(post_closes) > 0 and np.min(post_closes) < middle_price and np.max(post_highs) <= max(high1_price, high2_price) + (0.1 * atr):
                        result.update({
                            "m_confirmed": True,
                            "m_high1": float(high1_price),
                            "m_high2": float(high2_price),
                            "m_middle": float(middle_price),
                            "m_high2_idx": int(high2_idx),
                        })
                        break

    return result


def analyze_market_structure(df: pd.DataFrame, profile: Dict,
                             swing_order_override: Optional[int] = None,
                             state_key: Optional[str] = None,
                             drop_forming: bool = False) -> Dict:
    if drop_forming and len(df) > 60:
        df = df.iloc[:-1].reset_index(drop=True)

    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    decimals = profile.get('decimals', 5)

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

        for i in range(len(closes)):
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
                    and (extreme_price - closes[i]) >= (3.0 * atr)
                )
                broke = struct_broke or ema_crash
                pending = pending + 1 if broke else 0

                if pending >= FLIP_CONFIRM_BARS:
                    true_peak_high_idx = cycle_start_idx + int(np.argmax(highs[cycle_start_idx : i + 1]))
                    is_bull = False
                    cycle_start_idx = true_peak_high_idx
                    extreme_price = lows[i]
                    protected_high_idx = true_peak_high_idx
                    pending = 0
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
                    and (closes[i] - extreme_price) >= (3.0 * atr)
                )
                broke = struct_broke or ema_surge
                pending = pending + 1 if broke else 0

                if pending >= FLIP_CONFIRM_BARS:
                    true_peak_low_idx = cycle_start_idx + int(np.argmin(lows[cycle_start_idx : i + 1]))
                    is_bull = True
                    cycle_start_idx = true_peak_low_idx
                    extreme_price = highs[i]
                    protected_low_idx = true_peak_low_idx
                    pending = 0
                    events.append({
                        "flip_idx":     i,
                        "flip_time":    int(dates[i]),
                        "bullish":      True,
                        "anchor_idx":   true_peak_low_idx,
                        "anchor_time":  int(dates[true_peak_low_idx]),
                        "anchor_price": float(lows[true_peak_low_idx]),
                    })

        return events, is_bull

    seed_idx = min(50, len(closes) - 1)
    seed_bullish = bool(ema_50_array[seed_idx] > ema_200_array[seed_idx])
    cycle_events, is_bullish = compute_structural_cycles(seed_bullish)

    matching_events = [e for e in cycle_events if e["bullish"] == is_bullish]
    if matching_events:
        latest_event       = matching_events[-1]
        fresh_flip_time    = latest_event["flip_time"]
        fresh_anchor_idx   = latest_event["anchor_idx"]
        fresh_anchor_time  = latest_event["anchor_time"]
        fresh_anchor_price = latest_event["anchor_price"]
    else:
        fresh_flip_time    = 0
        fresh_anchor_idx   = int(np.argmin(lows)) if is_bullish else int(np.argmax(highs))
        fresh_anchor_time  = int(dates[fresh_anchor_idx])
        fresh_anchor_price = float(lows[fresh_anchor_idx]) if is_bullish else float(highs[fresh_anchor_idx])

    current_idx = len(closes) - 1
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

    # ── Strict Anti-Bounce Cycle & Anchor Lock ──────────────────────────────
    if state_key:
        st = CYCLE_STATE.get(state_key)
        if st is None:
            CYCLE_STATE[state_key] = {
                "bullish":      is_bullish,
                "flip_time":    fresh_flip_time,
                "anchor_time":  fresh_anchor_time,
                "anchor_price": fresh_anchor_price,
                "confirmed":    False,
            }
            st = CYCLE_STATE[state_key]
        else:
            if is_bullish != st["bullish"] and fresh_flip_time > st["flip_time"]:
                CYCLE_STATE[state_key] = {
                    "bullish":      is_bullish,
                    "flip_time":    fresh_flip_time,
                    "anchor_time":  fresh_anchor_time,
                    "anchor_price": fresh_anchor_price,
                    "confirmed":    False,
                }
                st = CYCLE_STATE[state_key]
            else:
                is_bullish = st["bullish"]
                locked_matches = np.where(dates == st["anchor_time"])[0]
                locked_idx = int(locked_matches[0]) if len(locked_matches) > 0 else 0
                if (current_idx - locked_idx) > 120 and fresh_anchor_time > st["anchor_time"]:
                    st["anchor_time"]  = fresh_anchor_time
                    st["anchor_price"] = fresh_anchor_price
                    st["confirmed"]    = False

        time_matches = np.where(dates == st["anchor_time"])[0]
        if len(time_matches) > 0:
            anchor_idx_found = int(time_matches[0])
        else:
            anchor_idx_found = int(np.searchsorted(dates, st["anchor_time"]))
            anchor_idx_found = int(np.clip(anchor_idx_found, 0, len(closes) - 1))

        if is_bullish:
            post_min_idx = anchor_idx_found + int(np.argmin(lows[anchor_idx_found:]))
            if lows[post_min_idx] < st["anchor_price"]:
                anchor_idx_found   = post_min_idx
                st["anchor_time"]  = int(dates[post_min_idx])
                st["anchor_price"] = float(lows[post_min_idx])
                st["confirmed"]    = False
        else:
            post_max_idx = anchor_idx_found + int(np.argmax(highs[anchor_idx_found:]))
            if highs[post_max_idx] > st["anchor_price"]:
                anchor_idx_found   = post_max_idx
                st["anchor_time"]  = int(dates[post_max_idx])
                st["anchor_price"] = float(highs[post_max_idx])
                st["confirmed"]    = False

        anchor_price = float(st["anchor_price"])
        anchor_time  = int(st["anchor_time"])
    else:
        anchor_idx_found = fresh_anchor_idx
        if is_bullish:
            post_min_idx = anchor_idx_found + int(np.argmin(lows[anchor_idx_found:]))
            if lows[post_min_idx] < lows[anchor_idx_found]:
                anchor_idx_found = post_min_idx
            anchor_price = float(lows[anchor_idx_found])
        else:
            post_max_idx = anchor_idx_found + int(np.argmax(highs[anchor_idx_found:]))
            if highs[post_max_idx] > highs[anchor_idx_found]:
                anchor_idx_found = post_max_idx
            anchor_price = float(highs[anchor_idx_found])
        anchor_time = int(dates[anchor_idx_found])

    # ── Candidate vs CONFIRMED peak ─────────────────────────────────────────
    if is_bullish:
        move = float(highs[anchor_idx_found:].max() - anchor_price)
        extreme_held = bool(lows[anchor_idx_found:].min() >= anchor_price - 1e-9)
    else:
        move = float(anchor_price - lows[anchor_idx_found:].min())
        extreme_held = bool(highs[anchor_idx_found:].max() <= anchor_price + 1e-9)

    peak_move_atr  = move / atr if atr > 0 else 0.0
    peak_confirmed = bool(extreme_held and move >= ANCHOR_MIN_MOVE_ATR * atr)

    if state_key and state_key in CYCLE_STATE:
        if peak_confirmed:
            CYCLE_STATE[state_key]["confirmed"] = True
        elif CYCLE_STATE[state_key]["confirmed"] and extreme_held:
            peak_confirmed = True

    if not is_bullish:
        cycle      = "BEARISH CYCLE (Peak M)"
        anchor_idx = anchor_idx_found
        if peak_confirmed:
            pattern_name = f"Peak Formation High ({anchor_price:.{decimals}f})"
            anchor_color = "rgba(255, 59, 59, 1)"
        else:
            pattern_name = f"Peak Candidate High ({anchor_price:.{decimals}f})"
            anchor_color = "rgba(255, 59, 59, 0.45)"
    else:
        cycle      = "BULLISH CYCLE (Peak W)"
        anchor_idx = anchor_idx_found
        if peak_confirmed:
            pattern_name = f"Peak Formation Low ({anchor_price:.{decimals}f})"
            anchor_color = "rgba(59, 255, 130, 1)"
        else:
            pattern_name = f"Peak Candidate Low ({anchor_price:.{decimals}f})"
            anchor_color = "rgba(59, 255, 130, 0.45)"

    sweeps = detect_liquidity_sweeps(df, raw_highs, raw_lows, atr)
    pattern_info = detect_double_pattern(df, raw_highs, raw_lows, atr, anchor_idx=anchor_idx)

    # ── Structural Targets (Target 1 & Target 2 like reference chart) ───────
    current_price_val = float(closes[-1])
    targets = find_structural_targets(
        df, raw_highs, raw_lows, is_bullish, current_price_val, anchor_price, atr, decimals
    )

    # 1. Lines: Peak Formation Line + Target 1 Line + Target 2 Line
    all_lines = [{
        "level":      round(anchor_price, decimals),
        "start_time": anchor_time,
        "end_time":   int(dates[-1]),
        "type":       pattern_name,
        "color":      anchor_color,
        "is_choch":   False
    }]
    target_line_color = "rgba(255, 193, 7, 0.95)"  # Gold/Tan institutional target color
    for t in targets:
        all_lines.append({
            "level":      t["level"],
            "start_time": t["start_time"],
            "end_time":   t["end_time"],
            "type":       t["label"],
            "color":      target_line_color,
            "is_choch":   False
        })

    # 2. Shaded Boxes (order_blocks array): Peak Base Zone + Target 1 & Target 2 Zones
    zone_boxes = []
    if is_bullish:
        zone_boxes.append({
            "type":      "BULL_OB",
            "top":       round(anchor_price + (0.28 * atr), decimals),
            "bottom":    round(anchor_price, decimals),
            "time":      anchor_time,
            "end_time":  int(dates[-1]),
            "label":     f"Peak Base Zone ({anchor_price:.{decimals}f})",
            "mitigated": False
        })
    else:
        zone_boxes.append({
            "type":      "BEAR_OB",
            "top":       round(anchor_price, decimals),
            "bottom":    round(anchor_price - (0.28 * atr), decimals),
            "time":      anchor_time,
            "end_time":  int(dates[-1]),
            "label":     f"Peak Supply Zone ({anchor_price:.{decimals}f})",
            "mitigated": False
        })

    for t in targets:
        zone_boxes.append({
            "type":      "BULL_OB" if is_bullish else "BEAR_OB",
            "top":       t["top"],
            "bottom":    t["bottom"],
            "time":      t["start_time"],
            "end_time":  t["end_time"],
            "label":     t["label"],
            "mitigated": False
        })

    # ── Strictly Unbroken Anchor-Pinned Trendline ───────────────────────────
    trendline_data = None
    swing_pts_idx = raw_lows if is_bullish else raw_highs
    swing_pts_idx = swing_pts_idx[(swing_pts_idx > anchor_idx) & (swing_pts_idx <= current_idx)]

    if len(swing_pts_idx) >= 1 and current_idx > anchor_idx:
        x1 = int(anchor_idx)
        y1 = float(lows[x1]) if is_bullish else float(highs[x1])
        best_x2 = None
        best_slope = None

        for x2_cand in swing_pts_idx:
            x2_cand = int(x2_cand)
            if x2_cand <= x1 + 2:
                continue
            y2_cand = float(lows[x2_cand]) if is_bullish else float(highs[x2_cand])
            slope_cand = (y2_cand - y1) / (x2_cand - x1)

            if is_bullish and slope_cand <= 0:
                continue
            if (not is_bullish) and slope_cand >= 0:
                continue

            indices = np.arange(x1 + 1, current_idx + 1)
            line_vals = y1 + slope_cand * (indices - x1)
            if is_bullish:
                if np.any(closes[x1 + 1 : current_idx + 1] < line_vals - (0.15 * atr)):
                    continue
            else:
                if np.any(closes[x1 + 1 : current_idx + 1] > line_vals + (0.15 * atr)):
                    continue

            if best_slope is None:
                best_slope = slope_cand
                best_x2 = x2_cand
            elif is_bullish and slope_cand < best_slope:
                best_slope = slope_cand
                best_x2 = x2_cand
            elif (not is_bullish) and slope_cand > best_slope:
                best_slope = slope_cand
                best_x2 = x2_cand

        if best_x2 is not None and best_slope is not None:
            end_level = float(y1 + best_slope * (current_idx - x1))
            trendline_data = {
                "start_time":  int(dates[x1]),
                "start_level": float(y1),
                "end_time":    int(dates[current_idx]),
                "end_level":   end_level,
                "type":        "Demand Trendline" if is_bullish else "Supply Trendline",
                "color":       "rgba(59, 255, 130, 0.8)" if is_bullish else "rgba(255, 59, 59, 0.8)",
            }

    # ── 2-Leg Projected Roadmap Arrow (Pullback -> Target 2) ────────────────
    step_seconds = int(dates[-1] - dates[-2]) if len(dates) >= 2 else 3600
    pullback_lvl = (
        trendline_data["end_level"] if trendline_data
        else (current_price_val - 0.55 * atr if is_bullish else current_price_val + 0.55 * atr)
    )
    projection_path = [
        {"time": int(dates[-1]), "price": round(current_price_val, decimals), "label": "Now"},
        {"time": int(dates[-1] + step_seconds * 4), "price": round(pullback_lvl, decimals), "label": "Pullback Zone"},
        {"time": int(dates[-1] + step_seconds * 16), "price": targets[-1]["level"], "label": targets[-1]["label"]},
    ]

    return {
        "cycle":           cycle,
        "is_bullish":      is_bullish,
        "lines":           all_lines,
        "anchor":          anchor_price,
        "anchor_idx":      int(anchor_idx),
        "atr":             atr,
        "peak_confirmed":  peak_confirmed,
        "peak_move_atr":   round(peak_move_atr, 2),
        "sweeps":          sweeps,
        "w_confirmed":     pattern_info["w_confirmed"],
        "m_confirmed":     pattern_info["m_confirmed"],
        "pattern_info":    pattern_info,
        "swing_order":     swing_order,
        "raw_highs":       raw_highs.tolist(),
        "raw_lows":        raw_lows.tolist(),
        "trendline":       trendline_data,
        "zone_boxes":      zone_boxes,
        "targets":         targets,
        "projection_path": projection_path,
    }


def evaluate_entry_signal(df: pd.DataFrame, ms: Dict, current_price: float,
                          ema_50: float, ema_200: float, rsi: float,
                          htf_aligned: bool, decimals: int) -> Dict:
    """
    Waits patiently for price to enter a reaction zone AND print a closed
    confirmation candle before activating a BUY or SELL signal.
    When activated, TP1 and TP2 are set directly to the chart's Target 1 and Target 2.
    """
    is_bullish     = ms["is_bullish"]
    peak_confirmed = ms["peak_confirmed"]
    anchor_price   = ms["anchor"]
    atr            = ms["atr"]
    trendline      = ms["trendline"]
    sweeps         = ms["sweeps"]
    w_confirmed    = ms["w_confirmed"]
    m_confirmed    = ms["m_confirmed"]
    targets        = ms["targets"]

    t1_level = targets[0]["level"]
    t2_level = targets[1]["level"]

    if not peak_confirmed or atr <= 0:
        return {
            "signal": "WAIT",
            "confidence": 25,
            "setup_name": "Waiting for Peak Confirmation",
            "entry_note": f"⏳ Peak Candidate forming at {anchor_price:.{decimals}f}. Waiting for {ANCHOR_MIN_MOVE_ATR} ATR displacement to confirm.",
            "trade_levels": None
        }

    last_open  = float(df['Open'].iloc[-1])
    last_close = float(df['Close'].iloc[-1])
    last_high  = float(df['High'].iloc[-1])
    last_low   = float(df['Low'].iloc[-1])
    prev_high  = float(df['High'].iloc[-2]) if len(df) > 1 else last_high
    prev_low   = float(df['Low'].iloc[-2]) if len(df) > 1 else last_low

    body_size      = abs(last_close - last_open)
    bullish_candle = (last_close > last_open) and (body_size >= 0.12 * atr)
    bearish_candle = (last_close < last_open) and (body_size >= 0.12 * atr)

    recent_sweep = None
    if sweeps:
        last_sw = sweeps[-1]
        if (len(df) - 1 - last_sw["sweep_idx"]) <= 4:
            if (is_bullish and last_sw["type"] == "BULL_SWEEP") or ((not is_bullish) and last_sw["type"] == "BEAR_SWEEP"):
                recent_sweep = last_sw

    reasons = []
    score   = 45

    if is_bullish:
        dist_to_anchor = current_price - anchor_price
        near_anchor_w  = (0.0 <= dist_to_anchor <= 1.25 * atr)
        near_tl        = False
        if trendline:
            tl_lvl = trendline["end_level"]
            near_tl = (min(last_low, prev_low) <= tl_lvl + 0.30 * atr) and (last_close >= tl_lvl)

        near_ema = (min(last_low, prev_low) <= max(ema_50, ema_200) + 0.20 * atr) and (last_close > ema_50)

        if near_anchor_w:
            score += 22
            reasons.append("Peak Base Zone retest")
        if near_tl:
            score += 20
            reasons.append("Demand Trendline bounce")
        if near_ema:
            score += 12
            reasons.append("50/200 EMA support hold")
        if recent_sweep:
            score += 18
            reasons.append("Bullish Stop-Hunt sweep")
        if w_confirmed:
            score += 10
            reasons.append("Confirmed W structure")
        if htf_aligned:
            score += 8

        in_reaction_zone = (near_anchor_w or near_tl or (near_ema and (recent_sweep is not None or w_confirmed)))
        if in_reaction_zone and bullish_candle and rsi < 66:
            sl_base   = anchor_price - (0.30 * atr) if near_anchor_w else min(last_low, prev_low) - (0.45 * atr)
            stop_loss = min(sl_base, current_price - (0.75 * atr))
            risk      = max(abs(current_price - stop_loss), 1e-6)
            rr        = round(abs(t2_level - current_price) / risk, 2)
            confidence = int(np.clip(score, 70, 96))
            setup_str  = " + ".join(reasons[:2]) if reasons else "Bullish Pullback Entry"
            return {
                "signal": "BUY",
                "confidence": confidence,
                "setup_name": setup_str,
                "entry_note": (
                    f"🟢 BUY SIGNAL ACTIVE ({setup_str}) — "
                    f"Entry: {current_price:.{decimals}f} | SL: {stop_loss:.{decimals}f} | "
                    f"Target 1: {t1_level:.{decimals}f} | Target 2: {t2_level:.{decimals}f}"
                ),
                "trade_levels": {
                    "entry":       round(current_price, decimals),
                    "stop_loss":   round(stop_loss, decimals),
                    "take_profit": round(t1_level, decimals),
                    "target_1":    round(t1_level, decimals),
                    "target_2":    round(t2_level, decimals),
                    "risk_reward": rr
                }
            }
        else:
            return {
                "signal": "WAIT",
                "confidence": int(np.clip(score, 40, 65)),
                "setup_name": "Bullish Roadmap Active — Waiting for Pullback Trigger",
                "entry_note": (
                    f"⏳ Peak Low locked at {anchor_price:.{decimals}f}. "
                    f"Upside Roadmap: Target 1 ({t1_level:.{decimals}f}) → Target 2 ({t2_level:.{decimals}f}). "
                    f"Waiting for pullback rejection candle to activate BUY."
                ),
                "trade_levels": None
            }

    else:
        dist_to_anchor = anchor_price - current_price
        near_anchor_m  = (0.0 <= dist_to_anchor <= 1.25 * atr)
        near_tl        = False
        if trendline:
            tl_lvl = trendline["end_level"]
            near_tl = (max(last_high, prev_high) >= tl_lvl - 0.30 * atr) and (last_close <= tl_lvl)

        near_ema = (max(last_high, prev_high) >= min(ema_50, ema_200) - 0.20 * atr) and (last_close < ema_50)

        if near_anchor_m:
            score += 22
            reasons.append("Peak Supply Zone retest")
        if near_tl:
            score += 20
            reasons.append("Supply Trendline rejection")
        if near_ema:
            score += 12
            reasons.append("50/200 EMA resistance hold")
        if recent_sweep:
            score += 18
            reasons.append("Bearish Stop-Hunt sweep")
        if m_confirmed:
            score += 10
            reasons.append("Confirmed M structure")
        if htf_aligned:
            score += 8

        in_reaction_zone = (near_anchor_m or near_tl or (near_ema and (recent_sweep is not None or m_confirmed)))
        if in_reaction_zone and bearish_candle and rsi > 34:
            sl_base   = anchor_price + (0.30 * atr) if near_anchor_m else max(last_high, prev_high) + (0.45 * atr)
            stop_loss = max(sl_base, current_price + (0.75 * atr))
            risk      = max(abs(stop_loss - current_price), 1e-6)
            rr        = round(abs(current_price - t2_level) / risk, 2)
            confidence = int(np.clip(score, 70, 96))
            setup_str  = " + ".join(reasons[:2]) if reasons else "Bearish Pullback Entry"
            return {
                "signal": "SELL",
                "confidence": confidence,
                "setup_name": setup_str,
                "entry_note": (
                    f"🔴 SELL SIGNAL ACTIVE ({setup_str}) — "
                    f"Entry: {current_price:.{decimals}f} | SL: {stop_loss:.{decimals}f} | "
                    f"Target 1: {t1_level:.{decimals}f} | Target 2: {t2_level:.{decimals}f}"
                ),
                "trade_levels": {
                    "entry":       round(current_price, decimals),
                    "stop_loss":   round(stop_loss, decimals),
                    "take_profit": round(t1_level, decimals),
                    "target_1":    round(t1_level, decimals),
                    "target_2":    round(t2_level, decimals),
                    "risk_reward": rr
                }
            }
        else:
            return {
                "signal": "WAIT",
                "confidence": int(np.clip(score, 40, 65)),
                "setup_name": "Bearish Roadmap Active — Waiting for Pullback Trigger",
                "entry_note": (
                    f"⏳ Peak High locked at {anchor_price:.{decimals}f}. "
                    f"Downside Roadmap: Target 1 ({t1_level:.{decimals}f}) → Target 2 ({t2_level:.{decimals}f}). "
                    f"Waiting for pullback rejection candle to activate SELL."
                ),
                "trade_levels": None
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

        ema_200 = safe_float(EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1], current_price)
        ema_50  = safe_float(EMAIndicator(close=df['Close'], window=50).ema_indicator().iloc[-1], current_price)
        rsi     = safe_float(RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1], 50.0)
        atr     = calculate_atr(df, 14)

        ema_bias = "ABOVE 200 EMA" if current_price > ema_200 else "BELOW 200 EMA"

        news_string = "No recent impactful news."
        if req.news_data:
            actual   = safe_float(req.news_data.get('actual', 0))
            forecast = safe_float(req.news_data.get('forecast', 0))
            event    = req.news_data.get('event', 'News Event')
            if actual > forecast:
                news_string = f"📰 {event}: Beat forecast ({actual} vs {forecast}). USD bullish."
            elif actual < forecast:
                news_string = f"📰 {event}: Missed forecast ({actual} vs {forecast}). USD bearish."

        ms = analyze_market_structure(
            df, profile,
            state_key=f"{req.currency}:{req.timeframe}",
            drop_forming=(DROP_FORMING_CANDLE and data_source == "LIVE_NODE_DATA"),
        )
        cycle           = ms['cycle']
        lines           = ms['lines']
        sweeps          = ms['sweeps']
        w_confirmed     = ms.get('w_confirmed', False)
        m_confirmed     = ms.get('m_confirmed', False)
        peak_confirmed  = ms.get('peak_confirmed', False)
        peak_move_atr   = ms.get('peak_move_atr', 0.0)
        trendline_data  = ms.get('trendline')
        zone_boxes      = ms.get('zone_boxes', [])
        targets         = ms.get('targets', [])
        projection_path = ms.get('projection_path', [])

        bias_str = 'BULLISH' if cycle.startswith('BULLISH') else 'BEARISH'

        htf_aligned = False
        if req.htf_candles and len(req.htf_candles) > 50:
            htf_df = process_live_candles(req.htf_candles)
            if htf_df is not None:
                htf_ema50  = safe_float(EMAIndicator(close=htf_df['Close'], window=50).ema_indicator().iloc[-1])
                htf_ema200 = safe_float(EMAIndicator(close=htf_df['Close'], window=200).ema_indicator().iloc[-1])
                htf_aligned = (cycle.startswith('BULLISH') and htf_ema50 > htf_ema200) or \
                              (cycle.startswith('BEARISH') and htf_ema50 < htf_ema200)
        else:
            htf_aligned = (cycle.startswith('BULLISH') and ema_50 > ema_200) or \
                          (cycle.startswith('BEARISH') and ema_50 < ema_200)

        eval_df = df.iloc[:-1].reset_index(drop=True) if (DROP_FORMING_CANDLE and data_source == "LIVE_NODE_DATA" and len(df) > 60) else df
        entry_eval = evaluate_entry_signal(
            eval_df, ms, current_price, ema_50, ema_200, rsi, htf_aligned, decimals
        )

        sweep_str = ""
        if sweeps:
            last_sweep = sweeps[-1]
            candle_age = len(df) - 1 - last_sweep['sweep_idx']
            if candle_age <= 15:
                sweep_str = f"🎯 Stop Hunt / Pin detected at {last_sweep['level']:.{decimals}f}."

        reasoning = [
            f"🧭 Market Maker Bias: {cycle}",
            f"📈 Price vs Macro Trend: {ema_bias}",
            news_string,
        ]
        if peak_confirmed:
            reasoning.append(f"✅ Peak CONFIRMED — price has displaced {peak_move_atr:.1f} ATR from the Peak Zone.")
        else:
            reasoning.append(f"⏳ Peak CANDIDATE — moved {peak_move_atr:.1f} of {ANCHOR_MIN_MOVE_ATR:.1f} ATR needed to confirm.")
        if targets:
            reasoning.append(f"🎯 Structural Roadmap: {targets[0]['label']} → {targets[1]['label']}")
        if sweep_str:
            reasoning.append(sweep_str)
        if cycle.startswith("BEARISH"):
            reasoning.append("✅ M pattern (double-top) confirmed." if m_confirmed else "⏳ Watching for M (double-top) retest.")
        else:
            reasoning.append("✅ W pattern (double-bottom) confirmed." if w_confirmed else "⏳ Watching for W (double-bottom) retest.")
        if trendline_data:
            reasoning.append(f"📐 {trendline_data['type']} active at {trendline_data['end_level']:.{decimals}f}.")
        reasoning.append(entry_eval["entry_note"])

        return {
            "signal":         entry_eval["signal"],
            "confidence":     entry_eval["confidence"],
            "trend":          cycle,
            "pattern":        entry_eval["setup_name"] if entry_eval["signal"] in ("BUY", "SELL") else ("Peak Formation" if peak_confirmed else "Peak Candidate"),
            "peak_confirmed": peak_confirmed,
            "reasoning":      reasoning,
            "keyLevels": {
                "resistance": targets[1]["level"] if ms["is_bullish"] else round(ms["anchor"], decimals),
                "support":    round(ms["anchor"], decimals) if ms["is_bullish"] else targets[1]["level"],
                "target1":    targets[0]["level"],
                "target2":    targets[1]["level"],
                "ema200":     round(ema_200, decimals),
                "ema50":      round(ema_50, decimals),
            },
            "visuals": {
                "bos_lines":       lines,
                "sweeps":          sweeps,
                "trendline":       trendline_data,
                "order_blocks":    zone_boxes,
                "targets":         targets,
                "projection_path": projection_path,
            },
            "mtf_confluence": [
                {"tf": "4H", "bias": "BULLISH" if htf_aligned and cycle.startswith("BULLISH") else "BEARISH" if htf_aligned else "NEUTRAL", "strength": 85 if htf_aligned else 40},
                {"tf": "1H", "bias": bias_str, "strength": 75 if bias_str == "BULLISH" else 65},
            ],
            "tradeSetup":     entry_eval["trade_levels"],
            "dataSource":     data_source,
        }

    except Exception as e:
        logger.error(f"Analysis Crash: {e}", exc_info=True)
        return {"signal": "ERROR", "confidence": 0, "reasoning": [f"Engine error: {str(e)}"]}


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG ENDPOINT (stateless — never touches the cycle lock)
# ─────────────────────────────────────────────────────────────────────────────

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

        ms = analyze_market_structure(df, profile)

        return {
            "✅ ENGINE VERSION":    "AuraBrain Peak Formation v5.0 (Visual Target Roadmap + Live Signal Activation)",
            "📊 INSTRUMENT":       req.currency,
            "💰 CURRENT PRICE":    round(current_price, profile['decimals']),
            "─── STRUCTURE ───": "──────────────────────────────────────",
            "🎯 CYCLE":            ms['cycle'],
            "⚓ ANCHOR":            ms['anchor'],
            "🎯 TARGETS":          ms.get('targets', []),
            "─── PEAK INFO ───": "──────────────────────────────────────",
            "Peak confirmed":              ms.get('peak_confirmed', False),
            "Peak move (ATR)":             ms.get('peak_move_atr', 0.0),
            "W (double-bottom) confirmed": ms.get('w_confirmed', False),
            "M (double-top) confirmed":    ms.get('m_confirmed', False),
            "Core Swing Highs":  ms.get('raw_highs', []),
            "Core Swing Lows":   ms.get('raw_lows', []),
            "Sweeps":            ms.get('sweeps', []),
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