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
# break only counts once a candle has actually CLOSED beyond the swing.
# Set to False ONLY if your node sends closed candles exclusively.
DROP_FORMING_CANDLE = True

# ─────────────────────────────────────────────────────────────────────────────
# PEAK CONFIRMATION TUNING
# ─────────────────────────────────────────────────────────────────────────────
# Causal fractal confirmation bars on the right side of a swing (3 to 6 bars)
BASE_SWING_ORDER             = 4
# A cycle flip only counts when price CLOSES beyond the opposing protected swing
# by this many ATRs, and does so for FLIP_CONFIRM_BARS closes in a row.
FLIP_BUFFER_ATR              = 0.25
FLIP_CONFIRM_BARS            = 2
# A peak is only shown as CONFIRMED once price has travelled at least this many
# ATRs away from it AND has not taken the extreme out since.
ANCHOR_MIN_MOVE_ATR          = 1.5
# A swing point must stand out by this many ATRs to count as structural.
SWING_PROMINENCE_ATR         = 0.90
# How close two lows/highs must be (in ATR) to count as a W / M (includes stop-hunts).
DOUBLE_PATTERN_TOLERANCE_ATR = 0.65

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}

# Off by default; flip to True only if you want order blocks displayed.
SHOW_ORDER_BLOCKS = False

# Anti-flicker & Anchor-Lock memory (per "currency:timeframe"):
#   CYCLE_STATE[key] = {
#       "bullish": bool,
#       "flip_time": int,
#       "anchor_time": int,
#       "anchor_price": float,
#       "confirmed": bool
#   }
#   ORDER_CACHE[key] = swing order currently in use (changes only on a real shift)
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
    except:
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
        except:
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
    cu = currency.upper().replace("/","").replace("-","").replace("_","")
    if cu in ("XAUUSD", "GOLD"):
        return {"decimals": 2, "pip_size": 0.01, "label": "XAU/USD"}
    if cu in ("XAGUSD", "SILVER"):
        return {"decimals": 3, "pip_size": 0.001, "label": "XAG/USD"}
    if "JPY" in cu:
        return {"decimals": 3, "pip_size": 0.01, "label": currency}
    if any(cu.startswith(p) or cu.endswith(p) for p in ("EUR","GBP","AUD","NZD","CAD","CHF","USD")):
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
    """
    Uses Kaufman Efficiency Ratio (KER) to adapt swing order between 3 and 6 bars.
    Clean trending/displacement -> 3-4 bars. Choppy consolidation -> 5-6 bars.
    """
    if len(df) < 30 or atr <= 0:
        return BASE_SWING_ORDER
    recent_closes = df['Close'].tail(20).values
    net_move = abs(recent_closes[-1] - recent_closes[0])
    path_length = np.sum(np.abs(np.diff(recent_closes)))
    efficiency = net_move / path_length if path_length > 0 else 0.5
    order = int(np.clip(round(6.0 - (efficiency * 3.0)), 3, 6))
    return order


def detect_swings_causal(highs: np.ndarray, lows: np.ndarray, swing_order: int, min_prominence: float):
    """
    Detects causal swing highs and lows without failing on equal highs/lows (tweezers).
    Left window looks back `swing_order * 2` bars; right window requires `swing_order` bars
    for fast, non-repainting confirmation.
    """
    n = len(highs)
    left_w = swing_order * 2
    right_w = swing_order
    filtered_highs = []
    filtered_lows = []

    for idx in range(left_w, n - right_w):
        # Swing High check: >= left window, > right window (handles equal-high tweezers)
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

        # Swing Low check: <= left window, < right window (handles equal-low tweezers)
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


def detect_order_blocks(df: pd.DataFrame, atr: float, bias: str, lookback: int = 150) -> List[Dict]:
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values

    obs = []
    start = max(0, len(closes) - lookback)

    for i in range(start + 2, len(closes) - 1):
        body_size = abs(closes[i] - closes[i-1])
        if body_size < atr * 0.3:
            continue

        if bias == 'BULLISH':
            is_bearish_candle = closes[i-1] < closes[i-2]
            strong_up_move    = closes[i] > highs[i-1] and (closes[i] - closes[i-1]) > atr * 0.8
            if is_bearish_candle and strong_up_move:
                obs.append({
                    'type':      'BULL_OB',
                    'top':       float(highs[i-1]),
                    'bottom':    float(lows[i-1]),
                    'time':      int(dates[i-1]),
                    'end_time':  int(dates[-1]),
                    'label':     'Bull OB',
                    'mitigated': float(lows[-1]) > float(lows[i-1])
                })
        else:
            is_bullish_candle = closes[i-1] > closes[i-2]
            strong_down_move  = closes[i] < lows[i-1] and (closes[i-1] - closes[i]) > atr * 0.8
            if is_bullish_candle and strong_down_move:
                obs.append({
                    'type':      'BEAR_OB',
                    'top':       float(highs[i-1]),
                    'bottom':    float(lows[i-1]),
                    'time':      int(dates[i-1]),
                    'end_time':  int(dates[-1]),
                    'label':     'Bear OB',
                    'mitigated': float(highs[-1]) < float(highs[i-1])
                })

    unmitigated = [ob for ob in obs if not ob['mitigated']]
    return unmitigated[-3:] if unmitigated else obs[-2:]


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
    """
    Confirms genuine W (Peak Formation Low) and M (Peak Formation High) chart patterns
    around the active cycle anchor or live market edge, including Stop-Hunt W/M variations.
    """
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

    # --- W pattern (double bottom) ---
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

    # --- M pattern (double top) ---
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
    # Ignore the still-forming candle so a break only counts on a CLOSED candle.
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
        # Require a shift of 3+ to change cached order so swings never flicker
        if cached is None or abs(fresh_order - cached) >= 3:
            ORDER_CACHE[state_key] = fresh_order
            cached = fresh_order
        swing_order = cached
    else:
        swing_order = fresh_order

    min_prominence = atr * SWING_PROMINENCE_ATR
    raw_highs, raw_lows = detect_swings_causal(highs, lows, swing_order, min_prominence)

    logger.info(f"Core MMM swings: order={swing_order}, prominence={min_prominence:.5f}")
    logger.info(f"Detected swing highs: {raw_highs.tolist()}")
    logger.info(f"Detected swing lows : {raw_lows.tolist()}")

    ema_50_array  = df['Close'].ewm(span=50, adjust=False).mean().values
    ema_200_array = df['Close'].ewm(span=200, adjust=False).mean().values

    def compute_structural_cycles(seed_bullish: bool):
        """
        Walks forward through price action tracking true Protected Swings.
        A Protected Low in an uptrend only advances when price makes a new Higher High
        (and vice versa in a downtrend). When the Protected Swing is broken by
        FLIP_CONFIRM_BARS closes beyond FLIP_BUFFER_ATR, a cycle flip occurs and the
        exact Peak Formation Anchor (the extreme prior to the break) is locked.
        """
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
                # Advance protected low ONLY when price achieves a new cycle high
                if highs[i] >= extreme_price:
                    extreme_price = highs[i]
                    if last_confirmed_low is not None and last_confirmed_low >= cycle_start_idx:
                        protected_low_idx = last_confirmed_low

                # If no confirmed low existed at the moment of the high, fallback to the first confirmed low of the cycle
                if protected_low_idx == cycle_start_idx and last_confirmed_low is not None and last_confirmed_low > cycle_start_idx:
                    if highs[i] - lows[last_confirmed_low] >= ANCHOR_MIN_MOVE_ATR * atr:
                        protected_low_idx = last_confirmed_low

                broke = closes[i] < (lows[protected_low_idx] - buf)
                pending = pending + 1 if broke else 0

                if pending >= FLIP_CONFIRM_BARS:
                    # Flip BULL -> BEAR: Peak High Anchor is the highest high from cycle_start_idx to i
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
                # Advance protected high ONLY when price achieves a new cycle low
                if lows[i] <= extreme_price:
                    extreme_price = lows[i]
                    if last_confirmed_high is not None and last_confirmed_high >= cycle_start_idx:
                        protected_high_idx = last_confirmed_high

                if protected_high_idx == cycle_start_idx and last_confirmed_high is not None and last_confirmed_high > cycle_start_idx:
                    if highs[last_confirmed_high] - lows[i] >= ANCHOR_MIN_MOVE_ATR * atr:
                        protected_high_idx = last_confirmed_high

                broke = closes[i] > (highs[protected_high_idx] + buf)
                pending = pending + 1 if broke else 0

                if pending >= FLIP_CONFIRM_BARS:
                    # Flip BEAR -> BULL: Peak Low Anchor is the lowest low from cycle_start_idx to i
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

    # Find the most recent cycle event matching the computed direction
    matching_events = [e for e in cycle_events if e["bullish"] == is_bullish]
    if matching_events:
        latest_event = matching_events[-1]
        fresh_flip_time    = latest_event["flip_time"]
        fresh_anchor_idx   = latest_event["anchor_idx"]
        fresh_anchor_time  = latest_event["anchor_time"]
        fresh_anchor_price = latest_event["anchor_price"]
    else:
        fresh_flip_time  = 0
        fresh_anchor_idx = int(np.argmin(lows)) if is_bullish else int(np.argmax(highs))
        fresh_anchor_time = int(dates[fresh_anchor_idx])
        fresh_anchor_price = float(lows[fresh_anchor_idx]) if is_bullish else float(highs[fresh_anchor_idx])

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
            # Case 1: Genuine new structural break in the OPPOSITE direction
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
                # Keep locked cycle direction and locked anchor!
                is_bullish = st["bullish"]

        # Map the locked anchor timestamp back to the current dataframe index
        time_matches = np.where(dates == st["anchor_time"])[0]
        if len(time_matches) > 0:
            anchor_idx_found = int(time_matches[0])
        else:
            # If the anchor candle is older than the start of the dataframe, pin to index 0
            anchor_idx_found = int(np.searchsorted(dates, st["anchor_time"]))
            anchor_idx_found = int(np.clip(anchor_idx_found, 0, len(closes) - 1))

        # One-way ratchet ONLY if price extends the extreme before a structural flip
        if is_bullish:
            post_min_idx = anchor_idx_found + int(np.argmin(lows[anchor_idx_found:]))
            if lows[post_min_idx] < st["anchor_price"]:
                anchor_idx_found   = post_min_idx
                st["anchor_time"]  = int(dates[post_min_idx])
                st["anchor_price"] = float(lows[post_min_idx])
        else:
            post_max_idx = anchor_idx_found + int(np.argmax(highs[anchor_idx_found:]))
            if highs[post_max_idx] > st["anchor_price"]:
                anchor_idx_found   = post_max_idx
                st["anchor_time"]  = int(dates[post_max_idx])
                st["anchor_price"] = float(highs[post_max_idx])

        anchor_price = float(st["anchor_price"])
        anchor_time  = int(st["anchor_time"])
    else:
        # Stateless mode (used by /api/debug)
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

    # Once confirmed in live state, keep it confirmed unless extreme is violated
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

    # ── Demand/supply trendline (convex hull of swing points since anchor) ──
    trendline_data = None
    swing_pts_idx = raw_lows if is_bullish else raw_highs
    swing_pts_idx = swing_pts_idx[(swing_pts_idx >= anchor_idx) & (swing_pts_idx <= len(closes) - 1)]
    if len(swing_pts_idx) >= 2:
        swing_prices = lows[swing_pts_idx] if is_bullish else highs[swing_pts_idx]
        pts = sorted(zip(swing_pts_idx.tolist(), swing_prices.tolist()))

        def _cross(o, a, b):
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

        def _hull(points, upper):
            seq = list(reversed(points)) if upper else points
            h = []
            for p in seq:
                while len(h) >= 2 and _cross(h[-2], h[-1], p) <= 0:
                    h.pop()
                h.append(p)
            if upper:
                h.reverse()
            return h

        hull = _hull(pts, upper=not is_bullish)
        if len(hull) >= 2:
            (x1, y1), (x2, y2) = hull[-2], hull[-1]
            if x2 != x1:
                slope = (y2 - y1) / (x2 - x1)
                intercept = y1 - slope * x1
                current_idx = len(closes) - 1
                trendline_data = {
                    "start_time":  int(dates[x1]),
                    "start_level": float(y1),
                    "end_time":    int(dates[current_idx]),
                    "end_level":   float(slope * current_idx + intercept),
                    "type":        "Demand Trendline" if is_bullish else "Supply Trendline",
                    "color":       "rgba(59, 255, 130, 0.8)" if is_bullish else "rgba(255, 59, 59, 0.8)",
                }

    # ── Nearest unmitigated Order Block in the cycle direction ──
    nearest_ob = None
    if SHOW_ORDER_BLOCKS:
        ob_bias = 'BULLISH' if is_bullish else 'BEARISH'
        obs_found = detect_order_blocks(df, atr, ob_bias)
        current_price = float(closes[-1])
        unmitigated = [ob for ob in obs_found if not ob['mitigated']]
        if unmitigated:
            if is_bullish:
                below = [ob for ob in unmitigated if ob['top'] <= current_price]
                if below:
                    nearest_ob = max(below, key=lambda ob: ob['top'])
            else:
                above = [ob for ob in unmitigated if ob['bottom'] >= current_price]
                if above:
                    nearest_ob = min(above, key=lambda ob: ob['bottom'])

    return {
        "cycle":          cycle,
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


def calculate_trade_levels(current_price: float, signal: str,
                           atr: float, decimals: int, ema_50: float) -> Optional[Dict]:
    try:
        entry = current_price
        if signal == "BUY":
            stop_loss   = min(entry - (atr * 1.5), ema_50 - (atr * 0.5))
            risk        = abs(entry - stop_loss)
            take_profit = entry + (risk * 2.0)
        elif signal == "SELL":
            stop_loss   = max(entry + (atr * 1.5), ema_50 + (atr * 0.5))
            risk        = abs(entry - stop_loss)
            take_profit = entry - (risk * 2.0)
        else:
            return None
        risk = abs(entry - stop_loss)
        if risk == 0:
            return None
        rr = round(abs(take_profit - entry) / risk, 2)
        return {
            "entry":       round(entry, decimals),
            "stop_loss":   round(stop_loss, decimals),
            "take_profit": round(take_profit, decimals),
            "risk_reward": rr
        }
    except Exception as e:
        logger.error(f"Trade level calc error: {e}")
        return None

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
        csv_last_price  = safe_float(df['Close'].iloc[-1])
        current_price   = req.current_price if req.current_price > 0 else csv_last_price

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
            reasoning.append(f"📐 {trendline_data['type']} plotted — where liquidity is likely to be swept before a reaction.")
        if nearest_ob:
            reasoning.append(f"🎯 Nearest Order Block: {nearest_ob['bottom']:.{decimals}f}–{nearest_ob['top']:.{decimals}f} — the real demand/supply zone.")
        reasoning.append("🚧 Entry signal in development — trendline and OB are shown for reference only, no trade signal generated yet.")

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

        return {
            "signal":     "NEUTRAL",
            "confidence": 0,
            "trend":      cycle,
            "pattern":    "Peak Formation" if peak_confirmed else "Peak Candidate",
            "peak_confirmed": peak_confirmed,
            "reasoning":  reasoning,
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
            "tradeSetup": None,
            "dataSource": data_source,
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
            "✅ ENGINE VERSION":    "AuraBrain Peak Formation v4.0 (Protected Swings + Timestamp Anchor Pinning)",
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