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
DEFAULT_RR_RATIO             = 2.2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}

# Only shows high-probability, unmitigated Order Blocks at structural origins with FVG
SHOW_ORDER_BLOCKS = True

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
# CORE ENGINE — Peak Formation & Market Structure
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


def detect_order_blocks(df: pd.DataFrame, atr: float, bias: str, anchor_idx: int) -> List[Dict]:
    """
    Institutional Order Block detector:
      1. Only searches AFTER (or at) the active Peak Formation Anchor (`anchor_idx`).
      2. Requires the OB candle to sit at a local structural extreme (swing origin),
         never floating in the middle of a chop zone.
      3. Requires a strong displacement + Fair Value Gap (FVG / imbalance).
      4. Caps box height at 1.0 ATR so giant volatile candles don't cover the chart.
      5. Strictly invalidates the OB if ANY subsequent candle closes beyond its 50% midpoint
         or wicks completely through the far edge.
    """
    opens  = df['Open'].values
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values

    obs = []
    total = len(closes)
    start = max(2, anchor_idx)

    for i in range(start, total - 2):
        ob_idx = i - 1
        if bias == 'BULLISH':
            # Must be a true local low origin (lowest low of surrounding 5 bars)
            win_start = max(0, ob_idx - 3)
            win_end   = min(total, ob_idx + 2)
            if lows[ob_idx] > np.min(lows[win_start:win_end]) + (0.15 * atr):
                continue

            # Impulse displacement + FVG (low of i+1 > high of ob_idx, or 2-bar surge > 1.2 ATR)
            displacement = (closes[i] - opens[i]) > (0.65 * atr) and closes[i] > highs[ob_idx]
            has_fvg = (lows[i + 1] > highs[ob_idx]) or ((closes[i + 1] - lows[ob_idx]) > 1.4 * atr)
            if not (displacement and has_fvg):
                continue

            bottom = float(lows[ob_idx])
            raw_top = float(max(opens[ob_idx], closes[ob_idx]))
            # Ensure reasonable thickness between 0.25 ATR and 0.95 ATR
            top = float(np.clip(raw_top, bottom + 0.25 * atr, bottom + 0.95 * atr))
            midpoint = (top + bottom) / 2.0

            # Strict invalidation: if any candle after the impulse closed below midpoint or wicked below bottom
            post_closes = closes[i + 1:]
            post_lows   = lows[i + 1:]
            invalidated = bool(np.min(post_closes) < midpoint or np.min(post_lows) < bottom - (0.05 * atr))
            if not invalidated:
                obs.append({
                    'type':      'BULL_OB',
                    'top':       top,
                    'bottom':    bottom,
                    'time':      int(dates[ob_idx]),
                    'end_time':  int(dates[-1]),
                    'label':     'Bull OB',
                    'mitigated': False
                })

        else:
            # Must be a true local high origin
            win_start = max(0, ob_idx - 3)
            win_end   = min(total, ob_idx + 2)
            if highs[ob_idx] < np.max(highs[win_start:win_end]) - (0.15 * atr):
                continue

            displacement = (opens[i] - closes[i]) > (0.65 * atr) and closes[i] < lows[ob_idx]
            has_fvg = (highs[i + 1] < lows[ob_idx]) or ((highs[ob_idx] - closes[i + 1]) > 1.4 * atr)
            if not (displacement and has_fvg):
                continue

            top = float(highs[ob_idx])
            raw_bottom = float(min(opens[ob_idx], closes[ob_idx]))
            bottom = float(np.clip(raw_bottom, top - 0.95 * atr, top - 0.25 * atr))
            midpoint = (top + bottom) / 2.0

            post_closes = closes[i + 1:]
            post_highs  = highs[i + 1:]
            invalidated = bool(np.max(post_closes) > midpoint or np.max(post_highs) > top + (0.05 * atr))
            if not invalidated:
                obs.append({
                    'type':      'BEAR_OB',
                    'top':       top,
                    'bottom':    bottom,
                    'time':      int(dates[ob_idx]),
                    'end_time':  int(dates[-1]),
                    'label':     'Bear OB',
                    'mitigated': False
                })

    return obs[-2:] if obs else []


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

                # Structural break OR severe macro breakdown below both EMAs (fixes USD/CAD waterfall crash)
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

    # ── Macro Cycle Expiration Reset (Only upgrades to a PROVEN same-direction swing) ──
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
                    st["confirmed"]    = False  # Always re-verify confirmation on a reset anchor

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
                st["confirmed"]    = False  # New lower low must re-earn confirmation
        else:
            post_max_idx = anchor_idx_found + int(np.argmax(highs[anchor_idx_found:]))
            if highs[post_max_idx] > st["anchor_price"]:
                anchor_idx_found   = post_max_idx
                st["anchor_time"]  = int(dates[post_max_idx])
                st["anchor_price"] = float(highs[post_max_idx])
                st["confirmed"]    = False  # New higher high must re-earn confirmation

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
            pattern_name = "Peak Formation High (M)"
            anchor_color = "rgba(255, 59, 59, 1)"
        else:
            pattern_name = "Peak Candidate High (M)"
            anchor_color = "rgba(255, 59, 59, 0.45)"
    else:
        cycle      = "BULLISH CYCLE (Peak W)"
        anchor_idx = anchor_idx_found
        if peak_confirmed:
            pattern_name = "Peak Formation Low (W)"
            anchor_color = "rgba(59, 255, 130, 1)"
        else:
            pattern_name = "Peak Candidate Low (W)"
            anchor_color = "rgba(59, 255, 130, 0.45)"

    sweeps = detect_liquidity_sweeps(df, raw_highs, raw_lows, atr)
    pattern_info = detect_double_pattern(df, raw_highs, raw_lows, atr, anchor_idx=anchor_idx)

    all_lines = [{
        "level":      anchor_price,
        "start_time": anchor_time,
        "end_time":   int(dates[-1]),
        "type":       pattern_name,
        "color":      anchor_color,
        "is_choch":   False
    }]

    # ── Strictly Unbroken Anchor-Pinned Trendline (Fixes USD/CHF & AUD/USD) ──
    # Checks EVERY candle between x1 and current_idx so a trendline that was
    # sliced through in the middle of the chart is disqualified.
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

            # Verify that NO candle between x1+1 and current_idx closed beyond the trendline
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

    # ── Nearest Unmitigated Structural Order Block ──────────────────────────
    nearest_ob = None
    if SHOW_ORDER_BLOCKS:
        ob_bias = 'BULLISH' if is_bullish else 'BEARISH'
        obs_found = detect_order_blocks(df, atr, ob_bias, anchor_idx=int(anchor_idx))
        current_price_val = float(closes[-1])
        if obs_found:
            if is_bullish:
                below = [ob for ob in obs_found if ob['bottom'] <= current_price_val + (0.3 * atr)]
                if below:
                    nearest_ob = max(below, key=lambda ob: ob['top'])
            else:
                above = [ob for ob in obs_found if ob['top'] >= current_price_val - (0.3 * atr)]
                if above:
                    nearest_ob = min(above, key=lambda ob: ob['bottom'])

    return {
        "cycle":          cycle,
        "is_bullish":     is_bullish,
        "lines":          all_lines,
        "anchor":         anchor_price,
        "anchor_idx":     int(anchor_idx),
        "atr":            atr,
        "peak_confirmed": peak_confirmed,
        "peak_move_atr":  round(peak_move_atr, 2),
        "sweeps":         sweeps,
        "w_confirmed":    pattern_info["w_confirmed"],
        "m_confirmed":    pattern_info["m_confirmed"],
        "pattern_info":   pattern_info,
        "swing_order":    swing_order,
        "raw_highs":      raw_highs.tolist(),
        "raw_lows":       raw_lows.tolist(),
        "trendline":      trendline_data,
        "nearest_ob":     nearest_ob,
    }


def evaluate_entry_signal(df: pd.DataFrame, ms: Dict, current_price: float,
                          ema_50: float, ema_200: float, rsi: float,
                          htf_aligned: bool, decimals: int) -> Dict:
    """
    Evaluates the 3 core Peak Formation entry setups on closed candles:
      1. 2nd-Leg M/W Retest of the Peak Formation zone
      2. Unbroken Trendline + 50/200 EMA Pullback Bounce
      3. Internal Liquidity Sweep + Unmitigated Structural Order Block tap
    """
    is_bullish     = ms["is_bullish"]
    peak_confirmed = ms["peak_confirmed"]
    anchor_price   = ms["anchor"]
    atr            = ms["atr"]
    trendline      = ms["trendline"]
    nearest_ob     = ms["nearest_ob"]
    sweeps         = ms["sweeps"]
    w_confirmed    = ms["w_confirmed"]
    m_confirmed    = ms["m_confirmed"]

    if not peak_confirmed or atr <= 0:
        return {
            "signal": "WAIT",
            "confidence": 25,
            "setup_name": "Waiting for Peak Confirmation",
            "entry_note": f"⏳ Wait for price to displace {ANCHOR_MIN_MOVE_ATR} ATR from the Peak Candidate before looking for entries.",
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
    score   = 40

    if is_bullish:
        dist_to_anchor = current_price - anchor_price
        near_anchor_w  = (0.0 <= dist_to_anchor <= 1.15 * atr)
        near_tl        = False
        if trendline:
            tl_lvl = trendline["end_level"]
            near_tl = (min(last_low, prev_low) <= tl_lvl + 0.30 * atr) and (last_close >= tl_lvl)

        near_ema = (min(last_low, prev_low) <= max(ema_50, ema_200) + 0.20 * atr) and (last_close > ema_50)
        in_ob    = False
        if nearest_ob:
            in_ob = (min(last_low, prev_low) <= nearest_ob["top"] + 0.10 * atr) and (last_close >= nearest_ob["bottom"])

        if near_anchor_w:
            score += 22
            reasons.append("2nd-Leg W retest near Peak Low zone")
        if near_tl:
            score += 20
            reasons.append("Demand Trendline bounce")
        if near_ema:
            score += 12
            reasons.append("50/200 EMA dynamic support hold")
        if in_ob:
            score += 18
            reasons.append("Unmitigated Bullish Order Block tap")
        if recent_sweep:
            score += 18
            reasons.append("Bullish Stop-Hunt sweep confirmed")
        if w_confirmed:
            score += 10
            reasons.append("Confirmed W neckline breakout")
        if htf_aligned:
            score += 8

        in_reaction_zone = (near_anchor_w or near_tl or in_ob or (near_ema and recent_sweep is not None))
        if in_reaction_zone and bullish_candle and rsi < 66:
            sl_base     = anchor_price - (0.35 * atr) if near_anchor_w else min(last_low, prev_low) - (0.45 * atr)
            stop_loss   = min(sl_base, current_price - (0.80 * atr))
            risk        = abs(current_price - stop_loss)
            take_profit = current_price + (risk * DEFAULT_RR_RATIO)
            confidence  = int(np.clip(score, 68, 96))
            setup_str   = " + ".join(reasons[:2]) if reasons else "Bullish Pullback Continuation"
            return {
                "signal": "BUY",
                "confidence": confidence,
                "setup_name": setup_str,
                "entry_note": f"🟢 BUY TRIGGERED ({setup_str}) — Entry: {current_price:.{decimals}f} | SL: {stop_loss:.{decimals}f} | TP: {take_profit:.{decimals}f}",
                "trade_levels": {
                    "entry":       round(current_price, decimals),
                    "stop_loss":   round(stop_loss, decimals),
                    "take_profit": round(take_profit, decimals),
                    "risk_reward": DEFAULT_RR_RATIO
                }
            }
        else:
            wait_target = f"Demand Trendline ({trendline['end_level']:.{decimals}f})" if trendline else f"50 EMA ({ema_50:.{decimals}f})"
            return {
                "signal": "WAIT",
                "confidence": int(np.clip(score, 35, 64)),
                "setup_name": "Bullish Cycle Active — Waiting for Pullback Trigger",
                "entry_note": f"⏳ Bullish Peak W locked at {anchor_price:.{decimals}f}. Wait for a pullback into {wait_target} + a bullish 1H close to BUY.",
                "trade_levels": None
            }

    else:
        dist_to_anchor = anchor_price - current_price
        near_anchor_m  = (0.0 <= dist_to_anchor <= 1.15 * atr)
        near_tl        = False
        if trendline:
            tl_lvl = trendline["end_level"]
            near_tl = (max(last_high, prev_high) >= tl_lvl - 0.30 * atr) and (last_close <= tl_lvl)

        near_ema = (max(last_high, prev_high) >= min(ema_50, ema_200) - 0.20 * atr) and (last_close < ema_50)
        in_ob    = False
        if nearest_ob:
            in_ob = (max(last_high, prev_high) >= nearest_ob["bottom"] - 0.10 * atr) and (last_close <= nearest_ob["top"])

        if near_anchor_m:
            score += 22
            reasons.append("2nd-Leg M retest near Peak High zone")
        if near_tl:
            score += 20
            reasons.append("Supply Trendline rejection")
        if near_ema:
            score += 12
            reasons.append("50/200 EMA dynamic resistance hold")
        if in_ob:
            score += 18
            reasons.append("Unmitigated Bearish Order Block tap")
        if recent_sweep:
            score += 18
            reasons.append("Bearish Stop-Hunt sweep confirmed")
        if m_confirmed:
            score += 10
            reasons.append("Confirmed M neckline breakdown")
        if htf_aligned:
            score += 8

        in_reaction_zone = (near_anchor_m or near_tl or in_ob or (near_ema and recent_sweep is not None))
        if in_reaction_zone and bearish_candle and rsi > 34:
            sl_base     = anchor_price + (0.35 * atr) if near_anchor_m else max(last_high, prev_high) + (0.45 * atr)
            stop_loss   = max(sl_base, current_price + (0.80 * atr))
            risk        = abs(stop_loss - current_price)
            take_profit = current_price - (risk * DEFAULT_RR_RATIO)
            confidence  = int(np.clip(score, 68, 96))
            setup_str   = " + ".join(reasons[:2]) if reasons else "Bearish Pullback Continuation"
            return {
                "signal": "SELL",
                "confidence": confidence,
                "setup_name": setup_str,
                "entry_note": f"🔴 SELL TRIGGERED ({setup_str}) — Entry: {current_price:.{decimals}f} | SL: {stop_loss:.{decimals}f} | TP: {take_profit:.{decimals}f}",
                "trade_levels": {
                    "entry":       round(current_price, decimals),
                    "stop_loss":   round(stop_loss, decimals),
                    "take_profit": round(take_profit, decimals),
                    "risk_reward": DEFAULT_RR_RATIO
                }
            }
        else:
            wait_target = f"Supply Trendline ({trendline['end_level']:.{decimals}f})" if trendline else f"50 EMA ({ema_50:.{decimals}f})"
            return {
                "signal": "WAIT",
                "confidence": int(np.clip(score, 35, 64)),
                "setup_name": "Bearish Cycle Active — Waiting for Pullback Trigger",
                "entry_note": f"⏳ Bearish Peak M locked at {anchor_price:.{decimals}f}. Wait for a rally into {wait_target} + a bearish 1H close to SELL.",
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
        cycle          = ms['cycle']
        lines          = ms['lines']
        sweeps         = ms['sweeps']
        w_confirmed    = ms.get('w_confirmed', False)
        m_confirmed    = ms.get('m_confirmed', False)
        peak_confirmed = ms.get('peak_confirmed', False)
        peak_move_atr  = ms.get('peak_move_atr', 0.0)
        trendline_data = ms.get('trendline')
        nearest_ob     = ms.get('nearest_ob')

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
            reasoning.append(f"✅ Peak CONFIRMED — price has moved {peak_move_atr:.1f} ATR away and the extreme is locked.")
        else:
            reasoning.append(f"⏳ Peak CANDIDATE — moved {peak_move_atr:.1f} of {ANCHOR_MIN_MOVE_ATR:.1f} ATR needed to confirm.")
        if sweep_str:
            reasoning.append(sweep_str)
        if cycle.startswith("BEARISH"):
            reasoning.append("✅ M pattern (double-top) confirmed." if m_confirmed else "⏳ No confirmed M (double-top) pattern yet.")
        else:
            reasoning.append("✅ W pattern (double-bottom) confirmed." if w_confirmed else "⏳ No confirmed W (double-bottom) pattern yet.")
        if trendline_data:
            reasoning.append(f"📐 {trendline_data['type']} active at {trendline_data['end_level']:.{decimals}f}.")
        if nearest_ob:
            reasoning.append(f"🎯 Unmitigated Order Block: {nearest_ob['bottom']:.{decimals}f}–{nearest_ob['top']:.{decimals}f}.")
        reasoning.append(entry_eval["entry_note"])

        return {
            "signal":         entry_eval["signal"],
            "confidence":     entry_eval["confidence"],
            "trend":          cycle,
            "pattern":        entry_eval["setup_name"] if entry_eval["signal"] in ("BUY", "SELL") else ("Peak Formation" if peak_confirmed else "Peak Candidate"),
            "peak_confirmed": peak_confirmed,
            "reasoning":      reasoning,
            "keyLevels": {
                "resistance": round(current_price + (atr * 2), decimals),
                "support":    round(current_price - (atr * 2), decimals),
                "ema200":     round(ema_200, decimals),
                "ema50":      round(ema_50, decimals),
            },
            "visuals": {
                "bos_lines":    lines,
                "sweeps":       sweeps,
                "trendline":    trendline_data,
                "order_blocks": [nearest_ob] if nearest_ob else [],
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
            "✅ ENGINE VERSION":    "AuraBrain Peak Formation v4.3 (Strict FVG Order Blocks + Unbroken Trendlines)",
            "📊 INSTRUMENT":       req.currency,
            "💰 CURRENT PRICE":    round(current_price, profile['decimals']),
            "─── STRUCTURE ───": "──────────────────────────────────────",
            "🎯 CYCLE":            ms['cycle'],
            "⚓ ANCHOR":            ms['anchor'],
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