import os
import pandas as pd
import numpy as np
import math
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
from scipy.signal import argrelextrema
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}

# Anti-flicker memory (per "currency:timeframe"):
#   CYCLE_STATE[key] = {"bullish": bool, "flip_time": int}
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
# IMPROVED PEAK DETECTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def adaptive_swing_order(df: pd.DataFrame, atr: float) -> int:
    avg_range = float((df['High'] - df['Low']).tail(50).mean())
    ratio = avg_range / atr if atr > 0 else 1.0
    order = int(np.clip(ratio * 10, 5, 20))
    return order

# ─────────────────────────────────────────────────────────────────────────────
# CORE ENGINE — Peak Formation
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
    high  = df['High'].values
    low   = df['Low'].values
    close = df['Close'].values
    tr_list = []
    for i in range(1, len(close)):
        tr = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i]  - close[i-1]))
        tr_list.append(tr)
    tr_series = pd.Series(tr_list)
    return float(tr_series.rolling(period).mean().iloc[-1]) if len(tr_list) >= period else float(tr_series.mean())

def detect_order_blocks(df: pd.DataFrame, atr: float, bias: str, lookback: int = 150) -> List[Dict]:
    """
    Order block: the last opposite-colored candle before a strong
    impulsive move — where the real order flow that drove the move
    originated. Repurposed per the strategy being tested: not for MMM
    scoring (removed), but as the actual demand/supply reaction zone,
    deeper and more reliable than the shallow retest of a broken
    resistance/support level that retail traders use.
    """
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
    min_wick = atr * 0.3
    total    = len(closes)
    cutoff   = max(0, total - 200)

    recent_highs = swing_highs[swing_highs >= cutoff]
    recent_lows  = swing_lows[swing_lows >= cutoff]

    for sh_idx in recent_highs:
        sh_price = highs[sh_idx]
        for i in range(sh_idx + 1, min(sh_idx + 80, total)):
            wick_above = highs[i] - sh_price
            if wick_above >= min_wick and closes[i] < sh_price:
                sweeps.append({
                    "type":      "BULL_SWEEP",
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
            if wick_below >= min_wick and closes[i] > sl_price:
                sweeps.append({
                    "type":      "BEAR_SWEEP",
                    "level":     float(sl_price),
                    "sweep_idx": int(i),
                    "time":      int(dates[i]) if len(dates) > i else 0,
                    "wick_size": float(wick_below),
                })
                break
    sweeps.sort(key=lambda x: x['sweep_idx'])
    return sweeps[-5:]


def detect_double_pattern(df: pd.DataFrame, swing_highs: np.ndarray, swing_lows: np.ndarray, atr: float, lookback: int = 150) -> Dict:
    """
    Confirms genuine W (double-bottom / Peak Formation Low) and M (double-top /
    Peak Formation High) chart patterns from real swing points, rather than
    relying purely on EMA trend direction (which only tells you the current
    bias, not whether an actual reversal shape has formed).
    """
    closes = df['Close'].values
    highs = df['High'].values
    lows = df['Low'].values
    total = len(closes)
    cutoff = max(0, total - lookback)

    price_tolerance = atr * 1.0      # how close the two lows/highs must be to count as "similar"
    middle_prominence = atr * 0.5    # how much higher/lower the middle swing point must be

    result = {
        "w_confirmed": False, "w_low1": None, "w_low2": None, "w_middle": None, "w_low2_idx": None,
        "m_confirmed": False, "m_high1": None, "m_high2": None, "m_middle": None, "m_high2_idx": None,
    }

    recent_lows = sorted([int(i) for i in swing_lows if i >= cutoff])
    recent_highs = sorted([int(i) for i in swing_highs if i >= cutoff])

    # --- W pattern (double bottom) ---
    if len(recent_lows) >= 2:
        low2_idx = recent_lows[-1]
        low1_idx = recent_lows[-2]
        low1_price = lows[low1_idx]
        low2_price = lows[low2_idx]

        if abs(low1_price - low2_price) <= price_tolerance:
            between_highs = [h for h in swing_highs if low1_idx < h < low2_idx]
            if between_highs:
                middle_idx = max(between_highs, key=lambda h: highs[int(h)])
                middle_price = highs[int(middle_idx)]
                if middle_price - max(low1_price, low2_price) >= middle_prominence:
                    if closes[-1] > low2_price:  # confirmation: price reclaimed above the second low
                        result["w_confirmed"] = True
                        result["w_low1"] = float(low1_price)
                        result["w_low2"] = float(low2_price)
                        result["w_middle"] = float(middle_price)
                        result["w_low2_idx"] = low2_idx

    # --- M pattern (double top) ---
    if len(recent_highs) >= 2:
        high2_idx = recent_highs[-1]
        high1_idx = recent_highs[-2]
        high1_price = highs[high1_idx]
        high2_price = highs[high2_idx]

        if abs(high1_price - high2_price) <= price_tolerance:
            between_lows = [l for l in swing_lows if high1_idx < l < high2_idx]
            if between_lows:
                middle_idx = min(between_lows, key=lambda l: lows[int(l)])
                middle_price = lows[int(middle_idx)]
                if min(high1_price, high2_price) - middle_price >= middle_prominence:
                    if closes[-1] < high2_price:  # confirmation: price dropped back below the second high
                        result["m_confirmed"] = True
                        result["m_high1"] = float(high1_price)
                        result["m_high2"] = float(high2_price)
                        result["m_middle"] = float(middle_price)
                        result["m_high2_idx"] = high2_idx

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
    if atr == 0: atr = float(df['Close'].mean()) * 0.001

    # swing_order_override exists ONLY for backtesting experiments (see
    # peak_speed_experiment.py) — it's never set by the live /api/analyze
    # endpoint, so live behavior is completely unchanged by this parameter.
    #
    # With a state_key (live), the order only changes when the adaptive value
    # has moved by 3+ — a shift of 1 candle in the order rewrites swing points
    # and was a source of flicker.
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
    min_prominence = atr * 0.3

    raw_highs_idx = argrelextrema(highs, np.greater, order=swing_order)[0]
    raw_lows_idx  = argrelextrema(lows,  np.less,    order=swing_order)[0]

    filtered_highs = []
    for idx in raw_highs_idx:
        if idx > swing_order and idx < len(highs) - swing_order:
            neigh_min = min(np.min(highs[idx-swing_order:idx]), np.min(highs[idx+1:idx+swing_order+1]))
            if highs[idx] - neigh_min >= min_prominence:
                filtered_highs.append(idx)

    filtered_lows = []
    for idx in raw_lows_idx:
        if idx > swing_order and idx < len(lows) - swing_order:
            neigh_max = max(np.max(lows[idx-swing_order:idx]), np.max(lows[idx+1:idx+swing_order+1]))
            if neigh_max - lows[idx] >= min_prominence:
                filtered_lows.append(idx)

    raw_highs = np.array(filtered_highs, dtype=int)
    raw_lows  = np.array(filtered_lows, dtype=int)

    logger.info(f"Core MMM swings: order={swing_order}, prominence={min_prominence:.5f}")
    logger.info(f"Detected swing highs: {raw_highs.tolist()}")
    logger.info(f"Detected swing lows : {raw_lows.tolist()}")

    ema_200_series = df['Close'].ewm(span=200, adjust=False).mean()
    ema_50_series  = df['Close'].ewm(span=50, adjust=False).mean()
    ema_50_array   = ema_50_series.values
    ema_200_array  = ema_200_series.values

    def compute_structural_flips(seed_bullish):
        """
        Walks forward through the whole series once, tracking the most
        recent CONFIRMED swing high/low, and records every index where a
        candle CLOSES beyond the opposing swing level — a Market
        Structure Break (MSB). This replaces EMA50/EMA200 as the cycle
        signal: EMA crossovers are a lagging indicator by construction.

        CAUSAL: a swing at index k only becomes usable at candle
        k + swing_order, because that is the earliest a live tool could have
        known it (it needs swing_order candles on its right). Using it any
        earlier is hindsight, and it makes past flips get rewritten when the
        swing is confirmed — the cause of the PFH/PFL flip-back.

        Confirmation requires a candle CLOSE beyond the swing level, not
        just a wick — a wick that pierces and retraces is a liquidity
        sweep, not a real structure break.

        Returns (flips, final_is_bullish).
        """
        highs_set = set(raw_highs.tolist())
        lows_set = set(raw_lows.tolist())
        flips = []
        is_bull = seed_bullish
        last_high_idx = None
        last_low_idx = None
        for i in range(len(closes)):
            if (i - swing_order) in highs_set:
                last_high_idx = i - swing_order
            if (i - swing_order) in lows_set:
                last_low_idx = i - swing_order
            if is_bull and last_low_idx is not None and closes[i] < lows[last_low_idx]:
                is_bull = False
                flips.append(i)
            elif not is_bull and last_high_idx is not None and closes[i] > highs[last_high_idx]:
                is_bull = True
                flips.append(i)
        return flips, is_bull

    seed_idx = min(300, len(closes) - 1)
    seed_bullish = ema_50_array[seed_idx] > ema_200_array[seed_idx]
    structural_flips, is_bullish = compute_structural_flips(seed_bullish)

    # ── Cycle lock ──────────────────────────────────────────────────────────
    # Only change the displayed cycle when there is a genuinely NEW break
    # (a flip later than the one already shown). If a recompute merely
    # disagrees with what was shown but has no newer break, it rewrote
    # history — keep the shown cycle.
    fresh_bullish = is_bullish
    last_flip_time = int(dates[structural_flips[-1]]) if structural_flips else 0
    if state_key:
        st = CYCLE_STATE.get(state_key)
        if st is None:
            CYCLE_STATE[state_key] = {"bullish": fresh_bullish, "flip_time": last_flip_time}
        elif fresh_bullish == st["bullish"]:
            st["flip_time"] = last_flip_time
        elif last_flip_time > st["flip_time"]:
            CYCLE_STATE[state_key] = {"bullish": fresh_bullish, "flip_time": last_flip_time}
        else:
            is_bullish = st["bullish"]

    def find_cycle_anchor(current_idx, is_bullish_now, max_lookback_crossings=10, edge_buffer=120):
        """
        Finds the true origin (highest high / lowest low) of the current
        cycle. A naive search bounded to [most-recent-flip, now] is wrong
        most of the time — the real peak/trough often sits just before
        the flip is confirmed, not after it.

        Steps backward through the precomputed structural flip list (most
        recent first) rather than crawling one candle at a time. Two
        failure modes are corrected once boundaries jump properly:
          1. Degenerate: the found extreme sits right on the flip candle
             itself (an artifact of where the window started).
          2. Edge-adjacent: a materially bigger extreme sits just outside
             the window, in the edge_buffer candles immediately before
             the flip.

        CONFIRMATION CAP (fixes early/premature Peak confirmation): the
        search window is never allowed to reach the last `swing_order`
        candles before the live edge. A brand-new low/high sitting right at
        the live edge hasn't proven anything yet — price could still be
        mid-move through it. Only once a bar has "survived" swing_order
        candles without being taken out is it eligible to be shown as the
        Peak. This uses the exact same confirmation window that already
        governs swing points and structural flips, so the whole engine is
        consistent about what counts as "confirmed."
        """
        confirmed_limit = max(0, current_idx - swing_order)

        candidate_boundaries = [f for f in reversed(structural_flips) if f <= current_idx]
        candidate_boundaries.append(0)  # always allow searching back to the start of available data

        extreme_idx = confirmed_limit
        for step in range(min(max_lookback_crossings, len(candidate_boundaries))):
            cross_idx_local = candidate_boundaries[step]
            search_end = max(cross_idx_local, confirmed_limit)
            if is_bullish_now:
                extreme_idx = cross_idx_local + int(np.argmin(lows[cross_idx_local:search_end + 1]))
            else:
                extreme_idx = cross_idx_local + int(np.argmax(highs[cross_idx_local:search_end + 1]))

            degenerate = (extreme_idx - cross_idx_local) <= 2

            edge_start = max(0, cross_idx_local - edge_buffer)
            bigger_nearby = False
            if edge_start < cross_idx_local:
                if is_bullish_now:
                    bigger_nearby = lows[edge_start:cross_idx_local].min() < lows[extreme_idx]
                else:
                    bigger_nearby = highs[edge_start:cross_idx_local].max() > highs[extreme_idx]

            if (not degenerate) and (not bigger_nearby):
                break
            if cross_idx_local == 0:
                break
        return extreme_idx

    anchor_idx_found = find_cycle_anchor(len(closes) - 1, is_bullish)

    if is_bullish:
        use_bearish  = False
        best_low_idx = anchor_idx_found
    else:
        use_bearish   = True
        best_high_idx = anchor_idx_found

    if use_bearish:
        cycle        = "BEARISH CYCLE (Peak M)"
        anchor_idx   = best_high_idx
        anchor_price = float(highs[anchor_idx])
        pattern_name = "Peak Formation High (M)"
        anchor_color = "rgba(255, 59, 59, 1)"
    else:
        cycle        = "BULLISH CYCLE (Peak W)"
        anchor_idx   = best_low_idx
        anchor_price = float(lows[anchor_idx])
        pattern_name = "Peak Formation Low (W)"
        anchor_color = "rgba(59, 255, 130, 1)"

    sweeps = detect_liquidity_sweeps(df, raw_highs, raw_lows, atr)
    pattern_info = detect_double_pattern(df, raw_highs, raw_lows, atr)

    all_lines = [{
        "level":      anchor_price,
        "start_time": int(dates[anchor_idx]),
        "end_time":   int(dates[-1]),
        "type":       pattern_name,
        "color":      anchor_color,
        "is_choch":   False
    }]

    # ── Demand/supply trendline (convex hull of swing points since anchor) ──
    # Fit through swing LOWS for a bullish/demand cycle, swing HIGHS for a
    # bearish/supply cycle. Uses the lower/upper convex hull, not a raw OLS
    # regression — a regression line lets roughly half its own defining
    # points sit on the wrong side of it, which doesn't behave like a real
    # support/resistance boundary. The hull guarantees every point used to
    # build it sits on the correct side, by construction.
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
    # The real reaction zone, per the strategy: not the shallow retest of a
    # broken level, but the last opposite-colored candle before the
    # impulsive move — where real order flow originated.
    nearest_ob = None
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
        "cycle":         cycle,
        "lines":         all_lines,
        "anchor":        anchor_price,
        "anchor_idx":    int(anchor_idx),
        "atr":           atr,
        "sweeps":        sweeps,
        "w_confirmed":   pattern_info["w_confirmed"],
        "m_confirmed":   pattern_info["m_confirmed"],
        "pattern_info":  pattern_info,
        "swing_order":   swing_order,
        "raw_highs":     raw_highs.tolist(),  # swing-high indices since the anchor — exposed for the upcoming trendline fit
        "raw_lows":      raw_lows.tolist(),   # swing-low indices — same purpose
        "trendline":     trendline_data,
        "nearest_ob":    nearest_ob,
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
        if risk == 0: return None
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
        cycle       = ms['cycle']
        lines       = ms['lines']
        sweeps      = ms['sweeps']
        w_confirmed = ms.get('w_confirmed', False)
        m_confirmed = ms.get('m_confirmed', False)
        trendline_data = ms.get('trendline')
        nearest_ob      = ms.get('nearest_ob')

        bias_str = 'BULLISH' if cycle.startswith('BULLISH') else 'BEARISH'

        sweep_nearby = False
        sweep_str = ""
        if sweeps:
            last_sweep = sweeps[-1]
            candle_age = len(df) - 1 - last_sweep['sweep_idx']
            if candle_age <= 15:
                sweep_nearby = True
                sweep_str = f"🎯 Stop Hunt / Pin detected at {last_sweep['level']:.{decimals}f}."

        reasoning = [
            f"🧭 Market Maker Bias: {cycle}",
            f"📈 Price vs Macro Trend: {ema_bias}",
            news_string,
        ]
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
            "pattern":    "Peak Formation",
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
        atr = calculate_atr(df, 14)

        ms = analyze_market_structure(df, profile)

        return {
            "✅ ENGINE VERSION":    "AuraBrain Peak Formation v3.2 (causal flips + cycle lock + anchor confirmation cap)",
            "📊 INSTRUMENT":       req.currency,
            "💰 CURRENT PRICE":    round(current_price, profile['decimals']),
            "─── STRUCTURE ───": "──────────────────────────────────────",
            "🎯 CYCLE":            ms['cycle'],
            "⚓ ANCHOR":            ms['anchor'],
            "─── PEAK INFO ───": "──────────────────────────────────────",
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