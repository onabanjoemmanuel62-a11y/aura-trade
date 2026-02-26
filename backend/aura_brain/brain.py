import os
import pandas as pd
import numpy as np
import math
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
from scipy.signal import argrelextrema
import logging
from contextlib import asynccontextmanager
import httpx
from starlette.responses import Response

from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

import joblib

CSV_FILENAME = "1h.csv"
NODE_URL = "http://127.0.0.1:10000"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}

base_dir = os.path.dirname(os.path.abspath(__file__))
model_path = os.path.join(base_dir, "aura_model.pkl")
try:
    ML_MODEL = joblib.load(model_path)
    logger.info("🧠 ML Brain loaded!")
except Exception as e:
    ML_MODEL = None
    logger.warning("⚠️ ML Brain not found. Rule-based confidence active.")

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
# CORE ENGINE  — "AURA SMC v10"
# ─────────────────────────────────────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """True ATR using high-low-prevclose, not just H-L."""
    high = df['High'].values
    low  = df['Low'].values
    close = df['Close'].values
    tr_list = []
    for i in range(1, len(close)):
        tr = max(high[i] - low[i],
                 abs(high[i] - close[i-1]),
                 abs(low[i]  - close[i-1]))
        tr_list.append(tr)
    tr_series = pd.Series(tr_list)
    return float(tr_series.rolling(period).mean().iloc[-1]) if len(tr_list) >= period else float(tr_series.mean())


def adaptive_swing_order(df: pd.DataFrame, atr: float) -> int:
    """
    Choose swing detection order based on average candle range vs ATR.
    Quieter markets → smaller order (detect smaller swings).
    Volatile markets → larger order (avoid noise peaks).
    """
    avg_range = float((df['High'] - df['Low']).tail(50).mean())
    ratio = avg_range / atr if atr > 0 else 1.0
    # ratio < 0.5 → very quiet → use order 5
    # ratio ≈ 1.0 → normal     → use order 10
    # ratio > 2.0 → very noisy → use order 20
    order = int(np.clip(ratio * 10, 5, 25))
    return order


def detect_liquidity_sweeps(df: pd.DataFrame, swing_highs: np.ndarray,
                             swing_lows: np.ndarray, atr: float) -> List[Dict]:
    """
    A liquidity sweep (stop hunt) occurs when price wicks THROUGH a prior
    swing high/low but the CANDLE BODY CLOSES BACK on the other side.
    This is the #1 trap retail traders fall into.
    """
    sweeps = []
    closes = df['Close'].values
    opens  = df['Open'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values

    # Build a simple list of prior swing levels to check against
    for sh_idx in swing_highs:
        sh_price = highs[sh_idx]
        # Look ahead for a sweep: wick above + close below
        for i in range(sh_idx + 1, min(sh_idx + 60, len(closes))):
            if highs[i] > sh_price and closes[i] < sh_price:
                sweeps.append({
                    "type": "BULL_SWEEP",      # Price swept sell-side liquidity above, reversed bearish
                    "level": float(sh_price),
                    "sweep_idx": int(i),
                    "time": int(dates[i]),
                    "wick_size": float(highs[i] - sh_price),
                })
                break  # Only record first sweep per swing high

    for sl_idx in swing_lows:
        sl_price = lows[sl_idx]
        for i in range(sl_idx + 1, min(sl_idx + 60, len(closes))):
            if lows[i] < sl_price and closes[i] > sl_price:
                sweeps.append({
                    "type": "BEAR_SWEEP",      # Price swept buy-side liquidity below, reversed bullish
                    "level": float(sl_price),
                    "sweep_idx": int(i),
                    "time": int(dates[i]),
                    "wick_size": float(sl_price - lows[i]),
                })
                break

    return sweeps


def find_displacement_candle(df: pd.DataFrame, start_idx: int, direction: str,
                              atr: float, lookback: int = 5) -> int:
    """
    A displacement candle is a large-body candle (>1.5× ATR body) that
    confirms institutional intent after a sweep or OB tap.
    Returns the index of the displacement candle, or -1 if not found.
    """
    closes = df['Close'].values
    opens  = df['Open'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    body_threshold = atr * 1.5

    for i in range(start_idx, min(start_idx + lookback, len(closes))):
        body = abs(closes[i] - opens[i])
        if body >= body_threshold:
            if direction == "DOWN" and closes[i] < opens[i]:
                return i
            elif direction == "UP" and closes[i] > opens[i]:
                return i
    return -1


def build_order_block(df: pd.DataFrame, anchor_idx: int, direction: str,
                      atr: float, dates) -> Optional[Dict]:
    """
    TRUE Order Block definition:
    - BEARISH OB: The LAST BULLISH candle before a strong bearish move
    - BULLISH OB: The LAST BEARISH candle before a strong bullish move

    We scan BACKWARDS from the anchor (sweep/BOS point) to find it.
    The OB body is used, NOT the full wick — wicks are stop hunt territory.
    """
    closes = df['Close'].values
    opens  = df['Open'].values
    highs  = df['High'].values
    lows   = df['Low'].values

    scan_start = max(0, anchor_idx - 10)  # Look back max 10 candles

    if direction == "BEAR":  # Find last bullish candle before bearish move
        for k in range(anchor_idx, scan_start, -1):
            if closes[k] > opens[k]:  # Bullish candle
                body_top    = max(closes[k], opens[k])
                body_bottom = min(closes[k], opens[k])
                ob_top    = body_top    + (atr * 0.1)   # Tiny wick allowance
                ob_bottom = body_bottom - (atr * 0.1)
                # Cap OB height to 2× ATR to avoid massive zones
                if (ob_top - ob_bottom) > atr * 2.0:
                    ob_bottom = ob_top - atr * 2.0
                return {
                    "type": "OB_BEAR",
                    "top": float(ob_top),
                    "bottom": float(ob_bottom),
                    "price": float(ob_top),       # Entry: top of bearish OB
                    "time": int(dates[k]),
                    "candle_idx": int(k),
                    "is_mitigated": False,
                    "fvg_size_pips": float(ob_top - ob_bottom),
                    "momentum_ratio": 2.0,
                    "label": "Bearish OB"
                }

    elif direction == "BULL":  # Find last bearish candle before bullish move
        for k in range(anchor_idx, scan_start, -1):
            if closes[k] < opens[k]:  # Bearish candle
                body_top    = max(closes[k], opens[k])
                body_bottom = min(closes[k], opens[k])
                ob_top    = body_top    + (atr * 0.1)
                ob_bottom = body_bottom - (atr * 0.1)
                if (ob_top - ob_bottom) > atr * 2.0:
                    ob_top = ob_bottom + atr * 2.0
                return {
                    "type": "OB_BULL",
                    "top": float(ob_top),
                    "bottom": float(ob_bottom),
                    "price": float(ob_bottom),    # Entry: bottom of bullish OB
                    "time": int(dates[k]),
                    "candle_idx": int(k),
                    "is_mitigated": False,
                    "fvg_size_pips": float(ob_top - ob_bottom),
                    "momentum_ratio": 2.0,
                    "label": "Bullish OB"
                }

    return None


def check_ob_mitigation(df: pd.DataFrame, ob: Dict, from_idx: int) -> bool:
    """
    An OB is mitigated (invalidated) when price CLOSES through its 50% level
    (the 'consequent encroachment' — the midpoint of the OB body).
    """
    closes = df['Close'].values
    midpoint = (ob['top'] + ob['bottom']) / 2.0
    for i in range(from_idx, len(closes)):
        if ob['type'] == "OB_BEAR" and closes[i] > midpoint:
            return True
        if ob['type'] == "OB_BULL" and closes[i] < midpoint:
            return True
    return False


def detect_choch(df: pd.DataFrame, swing_highs: np.ndarray,
                 swing_lows: np.ndarray, current_cycle: str) -> Dict:
    """
    Change of Character (CHoCH): The FIRST structural shift AGAINST the
    current trend. Different from BOS (which CONFIRMS the trend).

    In a BEARISH cycle: CHoCH = price closes ABOVE the most recent lower high
    In a BULLISH cycle: CHoCH = price closes BELOW the most recent higher low
    """
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values

    result = {"detected": False, "level": 0.0, "time": 0, "idx": -1}

    if current_cycle == "BEARISH" and len(swing_highs) >= 2:
        # Last lower high
        last_lower_high_price = highs[swing_highs[-1]]
        last_lower_high_idx   = swing_highs[-1]
        for i in range(last_lower_high_idx + 1, len(closes)):
            if closes[i] > last_lower_high_price:
                result = {
                    "detected": True,
                    "level": float(last_lower_high_price),
                    "time": int(dates[i]),
                    "idx": int(i)
                }
                break

    elif current_cycle == "BULLISH" and len(swing_lows) >= 2:
        last_higher_low_price = lows[swing_lows[-1]]
        last_higher_low_idx   = swing_lows[-1]
        for i in range(last_higher_low_idx + 1, len(closes)):
            if closes[i] < last_higher_low_price:
                result = {
                    "detected": True,
                    "level": float(last_higher_low_price),
                    "time": int(dates[i]),
                    "idx": int(i)
                }
                break

    return result


def detect_fair_value_gaps(df: pd.DataFrame, atr: float) -> List[Dict]:
    """
    FVG: A 3-candle imbalance where candle[i-2].high < candle[i].low (bullish FVG)
    or candle[i-2].low > candle[i].high (bearish FVG).
    Only return SIGNIFICANT FVGs (gap > 0.3× ATR).
    """
    fvgs = []
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    min_gap = atr * 0.3

    for i in range(2, len(highs)):
        bull_gap = lows[i] - highs[i-2]
        bear_gap = lows[i-2] - highs[i]

        if bull_gap > min_gap:
            fvgs.append({
                "type": "FVG_BULL",
                "top": float(lows[i]),
                "bottom": float(highs[i-2]),
                "time": int(dates[i-1]),
                "gap_size": float(bull_gap)
            })
        elif bear_gap > min_gap:
            fvgs.append({
                "type": "FVG_BEAR",
                "top": float(lows[i-2]),
                "bottom": float(highs[i]),
                "time": int(dates[i-1]),
                "gap_size": float(bear_gap)
            })

    # Only return the 3 most recent significant FVGs
    return fvgs[-3:] if fvgs else []


def analyze_market_structure(df: pd.DataFrame) -> Dict:
    """
    MASTER STRUCTURE ANALYSIS
    ──────────────────────────
    1. Adaptive swing detection (not hard-coded order=40)
    2. True Peak Formation High/Low identification
    3. BOS vs CHoCH differentiation
    4. Proper OB placement (last opposing candle before impulse)
    5. Liquidity sweep detection (stop hunt awareness)
    6. FVG mapping
    7. Multi-level MMM cycle tracking
    """
    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    opens  = df['Open'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values

    atr = calculate_atr(df, 14)
    if atr == 0:
        atr = float(df['Close'].mean()) * 0.001

    swing_order = adaptive_swing_order(df, atr)
    logger.info(f"Swing detection order: {swing_order} (ATR={atr:.4f})")

    # Raw swing pivots
    raw_highs = argrelextrema(highs, np.greater, order=swing_order)[0]
    raw_lows  = argrelextrema(lows,  np.less,    order=swing_order)[0]

    if len(raw_highs) == 0: raw_highs = np.array([int(np.argmax(highs))])
    if len(raw_lows)  == 0: raw_lows  = np.array([int(np.argmin(lows))])

    # ── STEP 1: FIND THE TRUE MACRO ANCHOR ────────────────────────────────────
    # The anchor is the highest-significance swing in recent history.
    # We look at the last 3 swing highs and 3 swing lows and pick the
    # most recent SIGNIFICANT one (not just the last chronologically).
    recent_highs = raw_highs[-4:] if len(raw_highs) >= 4 else raw_highs
    recent_lows  = raw_lows[-4:]  if len(raw_lows)  >= 4 else raw_lows

    last_high_idx = int(recent_highs[-1])
    last_low_idx  = int(recent_lows[-1])

    # The ANCHOR is the MOST RECENTLY FORMED significant swing
    if last_high_idx > last_low_idx:
        cycle         = "BEARISH"
        anchor_idx    = last_high_idx
        anchor_price  = float(highs[anchor_idx])
        pattern_name  = "PFH ↓ (Anchor)"
        anchor_color  = "rgba(255, 59, 59, 1)"
    else:
        cycle         = "BULLISH"
        anchor_idx    = last_low_idx
        anchor_price  = float(lows[anchor_idx])
        pattern_name  = "PFL ↑ (Anchor)"
        anchor_color  = "rgba(59, 255, 130, 1)"

    # ── STEP 2: LIQUIDITY SWEEP DETECTION ────────────────────────────────────
    sweeps = detect_liquidity_sweeps(df, raw_highs, raw_lows, atr)
    # Recent sweeps only (last 5)
    recent_sweeps = sweeps[-5:] if sweeps else []

    # ── STEP 3: BOS MAPPING + OB BUILDING ─────────────────────────────────────
    zones       = []
    bos_lines   = []
    choch_lines = []
    current_level = 0
    state = "IMPULSE"

    # Track swing extremes as we iterate
    if cycle == "BEARISH":
        ref_extreme_val = float(highs[anchor_idx])  # Peak to beat downward
        ref_extreme_idx = anchor_idx
        pb_extreme_val  = -np.inf                   # Pullback high
        pb_extreme_idx  = anchor_idx

        for i in range(anchor_idx + 1, len(closes)):
            if state == "IMPULSE":
                if lows[i] < ref_extreme_val:
                    ref_extreme_val = float(lows[i])
                    ref_extreme_idx = i
                # Pullback starts when price moves up > 0.5× ATR from the low
                elif highs[i] > ref_extreme_val + (atr * 0.5):
                    state = "PULLBACK"
                    pb_extreme_val = float(highs[i])
                    pb_extreme_idx = i

            elif state == "PULLBACK":
                if highs[i] > pb_extreme_val:
                    pb_extreme_val = float(highs[i])
                    pb_extreme_idx = i

                # BOS DOWN: body close below the last swing low (not just a wick)
                body_close = min(closes[i], opens[i])  # Bottom of body
                if body_close < ref_extreme_val:
                    current_level += 1

                    # Find displacement candle
                    disp_idx = find_displacement_candle(df, i, "DOWN", atr)

                    bos_lines.append({
                        "level": float(ref_extreme_val),
                        "start_time": int(dates[ref_extreme_idx]),
                        "end_time": int(dates[i]),
                        "type": f"BOS {current_level}",
                        "color": "rgba(33, 150, 243, 0.9)",
                        "is_choch": False
                    })

                    # Build OB from the pullback — last bullish candle before the drop
                    ob = build_order_block(df, pb_extreme_idx, "BEAR", atr, dates)
                    if ob:
                        # Check if already mitigated
                        ob['is_mitigated'] = check_ob_mitigation(df, ob, ob['candle_idx'] + 1)
                        if not ob['is_mitigated']:
                            zones.append(ob)

                    ref_extreme_val = float(lows[i])
                    ref_extreme_idx = i
                    state = "IMPULSE"

    elif cycle == "BULLISH":
        ref_extreme_val = float(lows[anchor_idx])
        ref_extreme_idx = anchor_idx
        pb_extreme_val  = np.inf
        pb_extreme_idx  = anchor_idx

        for i in range(anchor_idx + 1, len(closes)):
            if state == "IMPULSE":
                if highs[i] > ref_extreme_val:
                    ref_extreme_val = float(highs[i])
                    ref_extreme_idx = i
                elif lows[i] < ref_extreme_val - (atr * 0.5):
                    state = "PULLBACK"
                    pb_extreme_val = float(lows[i])
                    pb_extreme_idx = i

            elif state == "PULLBACK":
                if lows[i] < pb_extreme_val:
                    pb_extreme_val = float(lows[i])
                    pb_extreme_idx = i

                # BOS UP: body close above the last swing high
                body_close = max(closes[i], opens[i])  # Top of body
                if body_close > ref_extreme_val:
                    current_level += 1

                    bos_lines.append({
                        "level": float(ref_extreme_val),
                        "start_time": int(dates[ref_extreme_idx]),
                        "end_time": int(dates[i]),
                        "type": f"BOS {current_level}",
                        "color": "rgba(33, 150, 243, 0.9)",
                        "is_choch": False
                    })

                    ob = build_order_block(df, pb_extreme_idx, "BULL", atr, dates)
                    if ob:
                        ob['is_mitigated'] = check_ob_mitigation(df, ob, ob['candle_idx'] + 1)
                        if not ob['is_mitigated']:
                            zones.append(ob)

                    ref_extreme_val = float(highs[i])
                    ref_extreme_idx = i
                    state = "IMPULSE"

    in_pullback = (state == "PULLBACK")

    # ── STEP 4: CHoCH DETECTION ───────────────────────────────────────────────
    choch = detect_choch(df, raw_highs, raw_lows, cycle)
    if choch['detected']:
        choch_lines.append({
            "level": choch['level'],
            "start_time": int(dates[anchor_idx]),
            "end_time": choch['time'],
            "type": "CHoCH ⚠️",
            "color": "rgba(255, 165, 0, 1)",
            "is_choch": True
        })

    # ── STEP 5: FVGs ──────────────────────────────────────────────────────────
    fvgs = detect_fair_value_gaps(df, atr)

    # ── STEP 6: ANCHOR LINE ───────────────────────────────────────────────────
    all_lines = [{
        "level": anchor_price,
        "start_time": int(dates[anchor_idx]),
        "end_time": int(dates[-1]),
        "type": pattern_name,
        "color": anchor_color
    }] + bos_lines + choch_lines

    # Only send the 3 most recent UNMITIGATED OBs to keep the chart clean
    clean_zones = [z for z in zones if not z['is_mitigated']][-3:]

    return {
        "cycle": cycle,
        "level": current_level + 1,
        "in_pullback": in_pullback,
        "zones": clean_zones,
        "fvgs": fvgs,
        "lines": all_lines,
        "anchor": anchor_price,
        "atr": atr,
        "sweeps": recent_sweeps,
        "choch": choch,
        "raw_swing_highs": [int(x) for x in raw_highs[-5:]],
        "raw_swing_lows":  [int(x) for x in raw_lows[-5:]]
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DECISION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def score_ob_quality(ob: Dict, current_price: float, atr: float,
                     rsi: float, cycle: str, sweep_nearby: bool) -> int:
    """
    Score an OB from 0-100 based on multiple confluence factors.
    Human traders mentally do this checklist — we automate it.
    """
    score = 50  # Base

    # 1. Price is inside or touching the OB (most important!)
    if ob['type'] == "OB_BULL":
        if ob['bottom'] <= current_price <= ob['top']:
            score += 25       # Inside OB
        elif current_price <= ob['top'] + (atr * 0.3):
            score += 10       # Approaching from above

    elif ob['type'] == "OB_BEAR":
        if ob['bottom'] <= current_price <= ob['top']:
            score += 25
        elif current_price >= ob['bottom'] - (atr * 0.3):
            score += 10

    # 2. Cycle alignment
    if (cycle == "BULLISH" and ob['type'] == "OB_BULL") or \
       (cycle == "BEARISH" and ob['type'] == "OB_BEAR"):
        score += 15

    # 3. RSI confluence
    if ob['type'] == "OB_BULL" and rsi < 40:
        score += 10   # Oversold at bullish OB = great
    elif ob['type'] == "OB_BEAR" and rsi > 60:
        score += 10   # Overbought at bearish OB = great

    # 4. Liquidity sweep nearby (HUGE confluence — stop hunt + OB = sniper entry)
    if sweep_nearby:
        score += 15

    # 5. Penalize if OB zone is too large (low precision)
    if ob['fvg_size_pips'] > atr * 3:
        score -= 10

    return min(score, 95)  # Cap at 95 — never claim 100%


def calculate_trade_levels(current_price: float, signal: str,
                            support: float, resistance: float,
                            atr: float, decimals: int,
                            ob: Optional[Dict] = None) -> Optional[Dict]:
    try:
        entry = current_price

        if signal == "BUY":
            # SL goes below the OB low, not just a generic ATR buffer
            if ob:
                stop_loss = ob['bottom'] - (atr * 0.5)
            else:
                stop_loss = support - (atr * 0.5) if support > 0 else entry - (atr * 1.5)
            # TP1 at 1:1.5, TP2 at 1:3 (next OB/resistance)
            risk = abs(entry - stop_loss)
            take_profit = resistance if (resistance > entry + risk) else entry + (risk * 2.5)

        elif signal == "SELL":
            if ob:
                stop_loss = ob['top'] + (atr * 0.5)
            else:
                stop_loss = resistance + (atr * 0.5) if resistance > 0 else entry + (atr * 1.5)
            risk = abs(entry - stop_loss)
            take_profit = support if (support > 0 and support < entry - risk) else entry - (risk * 2.5)
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
    # ── DATA LOADING ──────────────────────────────────────────────────────────
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

        decimals = 5
        if current_price > 500:  decimals = 2
        elif current_price > 10: decimals = 3

        # ── INDICATORS ────────────────────────────────────────────────────────
        ema_200 = safe_float(
            EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1],
            current_price
        )
        ema_50 = safe_float(
            EMAIndicator(close=df['Close'], window=50).ema_indicator().iloc[-1],
            current_price
        )
        rsi = safe_float(
            RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1],
            50.0
        )
        atr = calculate_atr(df, 14)

        # ── MARKET STRUCTURE ──────────────────────────────────────────────────
        ms = analyze_market_structure(df)
        cycle         = ms['cycle']
        current_level = ms['level']
        in_pullback   = ms['in_pullback']
        smc_zones     = ms['zones']
        bos_lines     = ms['lines']
        fvgs          = ms['fvgs']
        sweeps        = ms['sweeps']
        choch         = ms['choch']

        master_bias = f"{cycle} CYCLE"

        # Closest OBs
        bull_obs = [z for z in smc_zones if z['type'] == 'OB_BULL']
        bear_obs = [z for z in smc_zones if z['type'] == 'OB_BEAR']

        nearest_bull_ob = bull_obs[-1] if bull_obs else None
        nearest_bear_ob = bear_obs[-1] if bear_obs else None

        sup_level = nearest_bull_ob['bottom'] if nearest_bull_ob else current_price - (atr * 2)
        res_level = nearest_bear_ob['top']    if nearest_bear_ob else current_price + (atr * 2)

        # ── NEWS BIAS ─────────────────────────────────────────────────────────
        news_val    = 0
        news_string = "No recent impactful news."
        if req.news_data:
            actual   = safe_float(req.news_data.get('actual', 0))
            forecast = safe_float(req.news_data.get('forecast', 0))
            event    = req.news_data.get('event', 'News Event')
            if actual > forecast:
                news_val    = -1   # USD positive → XAU negative
                news_string = f"📰 {event}: Beat forecast ({actual} vs {forecast}). USD bullish."
            elif actual < forecast:
                news_val    = 1
                news_string = f"📰 {event}: Missed forecast ({actual} vs {forecast}). USD bearish."

        # ── EMA TREND FILTER ──────────────────────────────────────────────────
        ema_bias = "ABOVE 200 EMA" if current_price > ema_200 else "BELOW 200 EMA"
        ema_trend_ok_bull = current_price > ema_200
        ema_trend_ok_bear = current_price < ema_200

        # ── CHoCH WARNING ─────────────────────────────────────────────────────
        choch_warning = ""
        if choch['detected']:
            if cycle == "BEARISH":
                choch_warning = "⚠️ CHoCH Detected: Bearish structure may be reversing UP."
            else:
                choch_warning = "⚠️ CHoCH Detected: Bullish structure may be reversing DOWN."

        # ── STOP HUNT / SWEEP AWARENESS ───────────────────────────────────────
        # Check if the most recent sweep aligns with our trade direction
        sweep_nearby = False
        sweep_str    = ""
        if sweeps:
            last_sweep = sweeps[-1]
            recent_enough = True  # Could add time filter here
            if recent_enough:
                if last_sweep['type'] == "BEAR_SWEEP" and cycle == "BULLISH":
                    sweep_nearby = True
                    sweep_str    = f"🎯 Buy-side liquidity swept at {last_sweep['level']:.{decimals}f} — potential reversal zone."
                elif last_sweep['type'] == "BULL_SWEEP" and cycle == "BEARISH":
                    sweep_nearby = True
                    sweep_str    = f"🎯 Sell-side liquidity swept at {last_sweep['level']:.{decimals}f} — potential reversal zone."

        # ── SIGNAL LOGIC ──────────────────────────────────────────────────────
        signal     = "NEUTRAL"
        confidence = 0
        target_ob  = None

        display_level = min(3, current_level)
        phase_string  = f"LEVEL {display_level} {'(PULLBACK)' if in_pullback else '(EXPANSION)'}"

        reasoning = [
            f"🧭 Master Bias: {master_bias}",
            f"📊 Phase: {phase_string}",
            f"📈 Price vs EMA: {ema_bias}",
            news_string,
        ]

        if choch_warning:
            reasoning.append(choch_warning)
        if sweep_str:
            reasoning.append(sweep_str)

        # Exhaustion check (too many levels deep = likely reversal incoming)
        if current_level > 4 and not in_pullback:
            reasoning.append("⏳ Structure Exhausted (Level 4+). Waiting for new anchor formation.")

        elif not in_pullback:
            reasoning.append("🔄 Expansion phase active. Waiting for pullback to OB before entering.")

        else:
            # ── BULLISH ENTRY ─────────────────────────────────────────────────
            if cycle == "BULLISH" and nearest_bull_ob:
                ob = nearest_bull_ob
                score = score_ob_quality(ob, current_price, atr, rsi, cycle, sweep_nearby)

                # Price is AT or INSIDE the bullish OB
                at_ob = ob['bottom'] - (atr * 0.3) <= current_price <= ob['top'] + (atr * 0.1)

                if at_ob:
                    if ema_trend_ok_bull:
                        signal    = "BUY"
                        target_ob = ob
                        confidence = score
                        reasoning.append(f"🔥 KILLZONE: Price tapped Bullish OB ({ob['bottom']:.{decimals}f}–{ob['top']:.{decimals}f})")
                        reasoning.append(f"✅ Trend confirmed: Above 200 EMA ({ema_200:.{decimals}f})")
                        if rsi < 45:
                            reasoning.append(f"✅ RSI oversold confluence ({rsi:.1f})")
                    else:
                        reasoning.append(f"🔥 OB tapped but below 200 EMA — reduced conviction BUY")
                        signal    = "BUY"
                        target_ob = ob
                        confidence = max(50, score - 15)  # Lower confidence

                elif current_price <= ob['top'] + atr:
                    reasoning.append(f"📍 Approaching Bullish OB. Waiting for tap ({ob['bottom']:.{decimals}f}–{ob['top']:.{decimals}f}).")
                else:
                    reasoning.append(f"📍 Pulling back toward Bullish OB at {ob['bottom']:.{decimals}f}.")

            # ── BEARISH ENTRY ─────────────────────────────────────────────────
            elif cycle == "BEARISH" and nearest_bear_ob:
                ob = nearest_bear_ob
                score = score_ob_quality(ob, current_price, atr, rsi, cycle, sweep_nearby)

                at_ob = ob['bottom'] - (atr * 0.1) <= current_price <= ob['top'] + (atr * 0.3)

                if at_ob:
                    if ema_trend_ok_bear:
                        signal    = "SELL"
                        target_ob = ob
                        confidence = score
                        reasoning.append(f"🔥 KILLZONE: Price tapped Bearish OB ({ob['bottom']:.{decimals}f}–{ob['top']:.{decimals}f})")
                        reasoning.append(f"✅ Trend confirmed: Below 200 EMA ({ema_200:.{decimals}f})")
                        if rsi > 55:
                            reasoning.append(f"✅ RSI overbought confluence ({rsi:.1f})")
                    else:
                        reasoning.append(f"🔥 OB tapped but above 200 EMA — reduced conviction SELL")
                        signal    = "SELL"
                        target_ob = ob
                        confidence = max(50, score - 15)

                elif current_price >= ob['bottom'] - atr:
                    reasoning.append(f"📍 Approaching Bearish OB. Waiting for tap ({ob['bottom']:.{decimals}f}–{ob['top']:.{decimals}f}).")
                else:
                    reasoning.append(f"📍 Rallying toward Bearish OB at {ob['top']:.{decimals}f}.")

            else:
                reasoning.append("⚠️ No valid OB found in pullback zone. Standing aside.")

        # ── ML OVERRIDE ───────────────────────────────────────────────────────
        if signal != "NEUTRAL" and target_ob and ML_MODEL:
            try:
                features = pd.DataFrame([{
                    'type':           1 if signal == "BUY" else 0,
                    'fvg_size_pips':  target_ob.get('fvg_size_pips', 0.0),
                    'rsi_at_entry':   rsi,
                    'atr_at_entry':   atr,
                    'momentum_ratio': target_ob.get('momentum_ratio', 1.0),
                    'news_bias':      news_val
                }])
                prob = ML_MODEL.predict_proba(features)[0][1]
                ml_conf = int(prob * 100)
                confidence = int((confidence + ml_conf) / 2)  # Blend rule + ML
                reasoning.append(f"🧠 ML Prediction: {ml_conf}% win probability.")
            except Exception as ml_e:
                logger.warning(f"ML inference failed: {ml_e}")

        if confidence == 0 and signal != "NEUTRAL":
            confidence = 60  # Minimum floor

        # ── TRADE SETUP ───────────────────────────────────────────────────────
        trade_setup = None
        if signal in ("BUY", "SELL") and confidence >= 65:
            trade_setup = calculate_trade_levels(
                current_price, signal, sup_level, res_level,
                atr, decimals, ob=target_ob
            )
            if trade_setup:
                reasoning.append(
                    f"📐 Setup: Entry {trade_setup['entry']} | "
                    f"SL {trade_setup['stop_loss']} | "
                    f"TP {trade_setup['take_profit']} | "
                    f"RR 1:{trade_setup['risk_reward']}"
                )

        # ── RESPONSE ──────────────────────────────────────────────────────────
        return {
            "signal":     signal,
            "confidence": int(confidence),
            "trend":      master_bias,
            "pattern":    "MMM Pullback + OB",
            "reasoning":  reasoning,
            "keyLevels": {
                "resistance": round(res_level, decimals),
                "support":    round(sup_level, decimals),
                "ema200":     round(ema_200, decimals),
                "ema50":      round(ema_50, decimals),
            },
            "visuals": {
                "smc_zones":  smc_zones,
                "bos_lines":  bos_lines,
                "fvgs":       fvgs,
                "sweeps":     recent_sweeps,
            },
            "tradeSetup":  trade_setup,
            "dataSource":  data_source,
        }

    except Exception as e:
        logger.error(f"Analysis Crash: {e}", exc_info=True)
        return {"signal": "ERROR", "confidence": 0, "reasoning": [f"Engine error: {str(e)}"]}


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
    except Exception:
        raise HTTPException(status_code=503, detail="Node proxy unavailable")