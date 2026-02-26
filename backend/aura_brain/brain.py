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
# ─────────────────────────────────────────────────────────────────────────────
# CORE ENGINE  — "AURA SMC v10"
# ─────────────────────────────────────────────────────────────────────────────

def get_instrument_profile(currency: str, current_price: float) -> Dict:
    """
    Returns pair-specific parameters so the engine works for ALL instruments.
    Every pair has different pip sizes, decimal precision, and OB size expectations.
    All thresholds are expressed as ATR multipliers — so they self-scale to any pair.
    """
    cu = currency.upper().replace("/","").replace("-","").replace("_","")

    # GOLD / SILVER — high price, large absolute moves
    if cu in ("XAUUSD", "GOLD"):
        return {"decimals": 2, "pip_size": 0.01, "ob_atr_cap": 1.0,
                "pullback_atr": 0.5, "min_bos_atr": 0.25, "label": "XAU/USD"}
    if cu in ("XAGUSD", "SILVER"):
        return {"decimals": 3, "pip_size": 0.001, "ob_atr_cap": 1.0,
                "pullback_atr": 0.5, "min_bos_atr": 0.25, "label": "XAG/USD"}

    # JPY pairs — price ~100-160, pip = 0.01
    if "JPY" in cu:
        return {"decimals": 3, "pip_size": 0.01, "ob_atr_cap": 1.2,
                "pullback_atr": 0.5, "min_bos_atr": 0.2, "label": currency}

    # Major forex (EUR, GBP, AUD, NZD, CAD, CHF, USD) — price 0.6-2.0, pip=0.0001
    if any(cu.startswith(p) or cu.endswith(p) for p in
           ("EUR","GBP","AUD","NZD","CAD","CHF","USD")):
        return {"decimals": 5, "pip_size": 0.0001, "ob_atr_cap": 1.2,
                "pullback_atr": 0.5, "min_bos_atr": 0.2, "label": currency}

    # High-price indices/crypto
    if current_price > 5000:
        return {"decimals": 1, "pip_size": 0.1, "ob_atr_cap": 1.0,
                "pullback_atr": 0.6, "min_bos_atr": 0.3, "label": currency}
    if current_price > 100:
        return {"decimals": 2, "pip_size": 0.01, "ob_atr_cap": 1.0,
                "pullback_atr": 0.5, "min_bos_atr": 0.25, "label": currency}

    # Fallback
    return {"decimals": 5, "pip_size": 0.0001, "ob_atr_cap": 1.2,
            "pullback_atr": 0.5, "min_bos_atr": 0.2, "label": currency}


def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """True ATR using high-low-prevclose, not just H-L."""
    high  = df['High'].values
    low   = df['Low'].values
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
    Swing detection order normalised by ATR — works identically on all pairs
    because it measures candle size relative to the pair's own volatility.
    """
    avg_range = float((df['High'] - df['Low']).tail(50).mean())
    ratio = avg_range / atr if atr > 0 else 1.0
    order = int(np.clip(ratio * 10, 5, 20))
    return order



def detect_liquidity_sweeps(df: pd.DataFrame, swing_highs: np.ndarray,
                             swing_lows: np.ndarray, atr: float) -> List[Dict]:
    """
    A liquidity sweep (stop hunt) occurs when price wicks THROUGH a prior
    swing high/low but the CANDLE BODY CLOSES BACK on the other side.

    Rules to avoid noise:
    - Wick must be > 0.3× ATR (significant, not random noise)
    - Only check swings from the last 200 candles (recent liquidity pools)
    - Sweep candle must occur within 80 bars of the original swing
    """
    sweeps = []
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    min_wick = atr * 0.3
    total    = len(closes)
    cutoff   = max(0, total - 200)  # Only recent swings matter

    recent_highs = swing_highs[swing_highs >= cutoff]
    recent_lows  = swing_lows[swing_lows   >= cutoff]

    for sh_idx in recent_highs:
        sh_price = highs[sh_idx]
        for i in range(sh_idx + 1, min(sh_idx + 80, total)):
            wick_above = highs[i] - sh_price
            if wick_above >= min_wick and closes[i] < sh_price:
                sweeps.append({
                    "type":      "BULL_SWEEP",
                    "level":     float(sh_price),
                    "sweep_idx": int(i),
                    "time":      int(dates[i]),
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
                    "time":      int(dates[i]),
                    "wick_size": float(wick_below),
                })
                break

    # Sort by time and only return last 5
    sweeps.sort(key=lambda x: x['sweep_idx'])
    return sweeps[-5:]


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


def build_order_block(df: pd.DataFrame, pb_extreme_idx: int, direction: str,
                      atr: float, dates, ob_atr_cap: float = 1.0,
                      bos_candle_idx: int = -1) -> Optional[Dict]:
    """
    TRUE Order Block — matches what a human SMC trader draws on the chart.

    For a BULLISH OB (like the USDJPY "AFTER" image):
      The OB is the LAST BEARISH candle in the pullback zone,
      specifically the one closest to where the impulse UP began.
      We scan FORWARD from the pullback extreme toward the BOS candle
      and find the last bearish candle before a sequence of bullish closes.

    For a BEARISH OB:
      The OB is the LAST BULLISH candle before the impulse DOWN.
      We scan forward from the pullback high toward the BOS candle.

    The zone = body of that candle only (no wick padding).
    Capped at ob_atr_cap × ATR to prevent massive rectangles.
    """
    closes = df['Close'].values
    opens  = df['Open'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    n      = len(closes)

    # Search window: from pullback extreme toward BOS candle (max 15 candles)
    end_idx = bos_candle_idx if (bos_candle_idx > pb_extreme_idx) else min(pb_extreme_idx + 15, n - 1)
    scan_range = range(pb_extreme_idx, min(end_idx + 1, n))

    ob_candle_idx = -1

    if direction == "BULL":
        # Find the LAST bearish candle in the window (last red before the green impulse)
        for k in scan_range:
            if closes[k] < opens[k]:  # Bearish candle
                ob_candle_idx = k

    elif direction == "BEAR":
        # Find the LAST bullish candle in the window (last green before the red impulse)
        for k in scan_range:
            if closes[k] > opens[k]:  # Bullish candle
                ob_candle_idx = k

    # Fallback: if no opposing candle found, use the extreme candle itself
    if ob_candle_idx == -1:
        ob_candle_idx = pb_extreme_idx

    body_top    = float(max(closes[ob_candle_idx], opens[ob_candle_idx]))
    body_bottom = float(min(closes[ob_candle_idx], opens[ob_candle_idx]))

    # Minimum zone size: at least 0.1× ATR so it's visible on chart
    min_size = atr * 0.1
    if (body_top - body_bottom) < min_size:
        mid = (body_top + body_bottom) / 2.0
        body_top    = mid + min_size / 2.0
        body_bottom = mid - min_size / 2.0

    # Cap maximum zone size
    if direction == "BULL" and (body_top - body_bottom) > atr * ob_atr_cap:
        body_top = body_bottom + (atr * ob_atr_cap)
    elif direction == "BEAR" and (body_top - body_bottom) > atr * ob_atr_cap:
        body_bottom = body_top - (atr * ob_atr_cap)

    if direction == "BULL":
        return {
            "type":          "OB_BULL",
            "top":           round(body_top, 8),
            "bottom":        round(body_bottom, 8),
            "price":         round(body_bottom, 8),  # Entry at bottom of bullish OB
            "time":          int(dates[ob_candle_idx]),
            "candle_idx":    int(ob_candle_idx),
            "is_mitigated":  False,
            "fvg_size_pips": float(body_top - body_bottom),
            "momentum_ratio": 2.0,
            "label":         "1H-OB (Bullish)",
            "entry_label":   "ONLY BUYS"
        }
    else:
        return {
            "type":          "OB_BEAR",
            "top":           round(body_top, 8),
            "bottom":        round(body_bottom, 8),
            "price":         round(body_top, 8),   # Entry at top of bearish OB
            "time":          int(dates[ob_candle_idx]),
            "candle_idx":    int(ob_candle_idx),
            "is_mitigated":  False,
            "fvg_size_pips": float(body_top - body_bottom),
            "momentum_ratio": 2.0,
            "label":         "1H-OB (Bearish)",
            "entry_label":   "ONLY SELLS"
        }


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


def analyze_market_structure(df: pd.DataFrame, profile: Dict) -> Dict:
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
    ob_atr_cap   = profile.get("ob_atr_cap", 1.0)
    pullback_atr = profile.get("pullback_atr", 0.5)

    swing_order = adaptive_swing_order(df, atr)
    logger.info(f"Swing detection order: {swing_order} (ATR={atr:.4f})")

    # Raw swing pivots
    raw_highs = argrelextrema(highs, np.greater, order=swing_order)[0]
    raw_lows  = argrelextrema(lows,  np.less,    order=swing_order)[0]

    if len(raw_highs) == 0: raw_highs = np.array([int(np.argmax(highs))])
    if len(raw_lows)  == 0: raw_lows  = np.array([int(np.argmin(lows))])

    # ── STEP 1: FIND THE TRUE MACRO ANCHOR ────────────────────────────────────
    # Score swings by CONSEQUENCE: how much did price move AFTER each swing?
    # This mirrors how a human looks at the chart — the peak that caused the
    # biggest drop is the real PFH anchor, not just the most recent swing high.

    ANCHOR_LOOKBACK = min(len(closes), 600)  # ~25 days on 1H
    search_highs = raw_highs[raw_highs >= len(closes) - ANCHOR_LOOKBACK]
    search_lows  = raw_lows[raw_lows   >= len(closes) - ANCHOR_LOOKBACK]

    if len(search_highs) == 0: search_highs = raw_highs[-3:]
    if len(search_lows)  == 0: search_lows  = raw_lows[-3:]

    # Score each swing high: how far did price DROP after it?
    best_high_score = -1.0
    best_high_idx   = int(search_highs[-1])
    for sh in search_highs:
        subsequent_low = float(np.min(lows[sh:])) if sh < len(lows) - 1 else float(highs[sh])
        drop = float(highs[sh]) - subsequent_low
        if drop > best_high_score:
            best_high_score = drop
            best_high_idx   = int(sh)

    # Score each swing low: how far did price RALLY after it?
    best_low_score = -1.0
    best_low_idx   = int(search_lows[-1])
    for sl in search_lows:
        subsequent_high = float(np.max(highs[sl:])) if sl < len(highs) - 1 else float(lows[sl])
        rally = subsequent_high - float(lows[sl])
        if rally > best_low_score:
            best_low_score = rally
            best_low_idx   = int(sl)

    # ── ANCHOR DECISION ──────────────────────────────────────────────────────
    # Pure consequence scoring — NO recency bias.
    # The anchor is whichever extreme caused the BIGGEST absolute price move
    # afterward. This is exactly how a human analyst identifies a macro anchor:
    # "what peak/trough started this whole trend?"
    #
    # Tiebreaker: if scores are within 10% of each other, prefer the more recent one
    # (it's the fresher structure).
    score_diff_pct = abs(best_high_score - best_low_score) / (max(best_high_score, best_low_score) + 1e-9)

    if score_diff_pct > 0.10:
        # Clear winner — use pure consequence score
        use_bearish = best_high_score > best_low_score
    else:
        # Too close — prefer the more recent anchor (tiebreaker)
        use_bearish = best_high_idx > best_low_idx

    if use_bearish:
        cycle        = "BEARISH"
        anchor_idx   = best_high_idx
        anchor_price = float(highs[anchor_idx])
        pattern_name = "PFH ↓ (Anchor)"
        anchor_color = "rgba(255, 59, 59, 1)"
    else:
        cycle        = "BULLISH"
        anchor_idx   = best_low_idx
        anchor_price = float(lows[anchor_idx])
        pattern_name = "PFL ↑ (Anchor)"
        anchor_color = "rgba(59, 255, 130, 1)"

    logger.info(f"Anchor: {cycle} @ {anchor_price:.2f} idx={anchor_idx} "
                f"H-score={best_high_score:.1f} L-score={best_low_score:.1f} "
                f"diff={score_diff_pct:.1%}")

    # ── STEP 2: LIQUIDITY SWEEP DETECTION ────────────────────────────────────
    sweeps = detect_liquidity_sweeps(df, raw_highs, raw_lows, atr)
    # Recent sweeps only (last 5)
    recent_sweeps = sweeps[-5:] if sweeps else []

    # ── STEP 3: BOS MAPPING + OB BUILDING ─────────────────────────────────────
    # KEY RULES (human trader logic):
    # 1. A BOS only counts if it breaks a SWING-VALIDATED extreme, not a raw candle low/high.
    #    We use the pre-computed swing_order argrelextrema arrays as the validated extremes.
    # 2. Min displacement: the BOS candle body must move > min_bos_atr × ATR past the level.
    #    This filters out 1-pip "breaks" that are really just noise.
    # 3. After a BOS, the new reference extreme is the NEXT swing low/high found AFTER
    #    the BOS candle — not the BOS candle's own low/high.
    # 4. Hard cap: max 4 BOS lines drawn. Beyond that = exhaustion, wait for new anchor.
    # 5. Pullback must retrace at least pullback_atr × ATR AND be confirmed by a swing pivot,
    #    not just a single candle moving in the opposite direction.

    MAX_BOS   = 4
    min_disp  = profile.get("min_bos_atr", 0.25) * atr  # Min displacement past level

    zones       = []
    bos_lines   = []
    choch_lines = []
    current_level = 0

    # Use pre-computed swing arrays as validated reference levels
    # Filter to only those AFTER the anchor
    post_anchor_sh = raw_highs[raw_highs > anchor_idx]
    post_anchor_sl = raw_lows[raw_lows   > anchor_idx]

    # ── TRUE BOS LOGIC ────────────────────────────────────────────────────────
    # The correct MMM / SMC sequence is:
    #
    #  BEARISH CYCLE:
    #    Anchor (PFH) → IMPULSE down to swing low L1 → PULLBACK up to swing high H1
    #    → price breaks BELOW L1 → THIS is BOS 1. The level broken is L1, NOT the anchor.
    #    Then: new impulse to L2 → pullback to H2 → break below L2 → BOS 2. Etc.
    #
    #  BULLISH CYCLE:
    #    Anchor (PFL) → IMPULSE up to swing high H1 → PULLBACK down to swing low L1
    #    → price breaks ABOVE H1 → THIS is BOS 1.
    #
    # So the algorithm is:
    #   Phase 0 — INITIAL IMPULSE: find the FIRST swing extreme after the anchor.
    #             This becomes our first "reference level" to be broken.
    #             (We do NOT draw a BOS for the initial impulse — it is just the setup.)
    #   Phase 1+ — For each subsequent swing extreme that EXCEEDS the prior:
    #             Find the pullback between them, build the OB, draw the BOS.

    if cycle == "BEARISH":
        if len(post_anchor_sl) == 0:
            pass  # No structure yet
        else:
            # Phase 0: First swing low after anchor = initial impulse reference
            # This is NOT a BOS — it's just where the market first showed weakness
            first_sl_idx   = int(post_anchor_sl[0])
            first_sl_price = float(lows[first_sl_idx])

            last_confirmed_low_idx = first_sl_idx
            last_confirmed_low_val = first_sl_price

            # Phase 1+: Walk remaining swing lows looking for each successive break
            for sl_idx in post_anchor_sl[1:]:  # Skip the first one (initial impulse)
                if current_level >= MAX_BOS:
                    break
                sl_price = float(lows[sl_idx])

                # Must break BELOW the last confirmed swing low (not just touch it)
                if sl_price >= last_confirmed_low_val:
                    # This swing low is HIGHER — could be a pullback low, not a new BOS
                    # Update the reference if this is a more recent higher low (no action)
                    continue

                # Find the pullback HIGH between last_confirmed_low and this new low
                # This is the swing high that formed AFTER the last low and BEFORE this new low
                pb_highs_in_window = post_anchor_sh[
                    (post_anchor_sh > last_confirmed_low_idx) &
                    (post_anchor_sh < sl_idx)
                ]
                if len(pb_highs_in_window) == 0:
                    # No swing high found — look at raw highest candle in window
                    window_slice = highs[last_confirmed_low_idx:sl_idx]
                    if len(window_slice) < 3:
                        # Window too small — skip, not a real BOS
                        last_confirmed_low_val = sl_price
                        last_confirmed_low_idx = sl_idx
                        continue
                    pb_extreme_idx = int(last_confirmed_low_idx + np.argmax(window_slice))
                else:
                    pb_extreme_idx = int(pb_highs_in_window[np.argmax(highs[pb_highs_in_window])])

                pb_extreme_val = float(highs[pb_extreme_idx])

                # Require a REAL pullback: the swing high must be meaningfully above the low
                # (at least pullback_atr × ATR above last_confirmed_low_val)
                if pb_extreme_val < last_confirmed_low_val + (atr * pullback_atr):
                    # Too shallow — not a real pullback, just noise. Skip BOS, update ref.
                    last_confirmed_low_val = sl_price
                    last_confirmed_low_idx = sl_idx
                    continue

                # Valid BOS: find the break candle
                bos_candle_idx = sl_idx
                for j in range(sl_idx, min(sl_idx + 5, len(closes))):
                    if closes[j] < (last_confirmed_low_val - min_disp):
                        bos_candle_idx = j
                        break

                current_level += 1
                bos_lines.append({
                    "level": float(last_confirmed_low_val),
                    "start_time": int(dates[last_confirmed_low_idx]),
                    "end_time": int(dates[bos_candle_idx]),
                    "type": f"BOS {current_level}",
                    "color": "rgba(33, 150, 243, 0.9)",
                    "is_choch": False
                })

                ob = build_order_block(df, pb_extreme_idx, "BEAR", atr, dates, ob_atr_cap, bos_candle_idx)
                if ob:
                    ob['is_mitigated'] = check_ob_mitigation(df, ob, ob['candle_idx'] + 1)
                    if not ob['is_mitigated']:
                        zones.append(ob)

                last_confirmed_low_val = sl_price
                last_confirmed_low_idx = sl_idx

    elif cycle == "BULLISH":
        if len(post_anchor_sh) == 0:
            pass
        else:
            # Phase 0: First swing high after anchor = initial impulse reference (NOT a BOS)
            first_sh_idx   = int(post_anchor_sh[0])
            first_sh_price = float(highs[first_sh_idx])

            last_confirmed_high_idx = first_sh_idx
            last_confirmed_high_val = first_sh_price

            # Phase 1+: Walk remaining swing highs
            for sh_idx in post_anchor_sh[1:]:
                if current_level >= MAX_BOS:
                    break
                sh_price = float(highs[sh_idx])

                if sh_price <= last_confirmed_high_val:
                    continue  # Lower high — not a BOS candidate

                # Find the pullback LOW between last confirmed high and this new high
                pb_lows_in_window = post_anchor_sl[
                    (post_anchor_sl > last_confirmed_high_idx) &
                    (post_anchor_sl < sh_idx)
                ]
                if len(pb_lows_in_window) == 0:
                    window_slice = lows[last_confirmed_high_idx:sh_idx]
                    if len(window_slice) < 3:
                        last_confirmed_high_val = sh_price
                        last_confirmed_high_idx = sh_idx
                        continue
                    pb_extreme_idx = int(last_confirmed_high_idx + np.argmin(window_slice))
                else:
                    pb_extreme_idx = int(pb_lows_in_window[np.argmin(lows[pb_lows_in_window])])

                pb_extreme_val = float(lows[pb_extreme_idx])

                # Require a real pullback
                if pb_extreme_val > last_confirmed_high_val - (atr * pullback_atr):
                    last_confirmed_high_val = sh_price
                    last_confirmed_high_idx = sh_idx
                    continue

                bos_candle_idx = sh_idx
                for j in range(sh_idx, min(sh_idx + 5, len(closes))):
                    if closes[j] > (last_confirmed_high_val + min_disp):
                        bos_candle_idx = j
                        break

                current_level += 1
                bos_lines.append({
                    "level": float(last_confirmed_high_val),
                    "start_time": int(dates[last_confirmed_high_idx]),
                    "end_time": int(dates[bos_candle_idx]),
                    "type": f"BOS {current_level}",
                    "color": "rgba(33, 150, 243, 0.9)",
                    "is_choch": False
                })

                ob = build_order_block(df, pb_extreme_idx, "BULL", atr, dates, ob_atr_cap, bos_candle_idx)
                if ob:
                    ob['is_mitigated'] = check_ob_mitigation(df, ob, ob['candle_idx'] + 1)
                    if not ob['is_mitigated']:
                        zones.append(ob)

                last_confirmed_high_val = sh_price
                last_confirmed_high_idx = sh_idx

    in_pullback = False
    # Determine current phase:
    # - If we have BOS lines: in_pullback = most recent swing is AGAINST the trend
    # - If NO BOS yet (still in phase 0 initial impulse): in_pullback = False (still expanding)
    if current_level == 0:
        # No BOS drawn yet — still in initial impulse. Not in pullback.
        in_pullback = False
    elif cycle == "BEARISH" and len(post_anchor_sh) > 0 and len(post_anchor_sl) > 0:
        # In pullback if the most recent swing HIGH came after the most recent swing LOW
        last_sh = int(post_anchor_sh[-1])
        last_sl = int(post_anchor_sl[-1])
        in_pullback = last_sh > last_sl
    elif cycle == "BULLISH" and len(post_anchor_sl) > 0 and len(post_anchor_sh) > 0:
        # In pullback if the most recent swing LOW came after the most recent swing HIGH
        last_sl = int(post_anchor_sl[-1])
        last_sh = int(post_anchor_sh[-1])
        in_pullback = last_sl > last_sh

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
        "cycle":           cycle,
        "level":           current_level + 1,
        "in_pullback":     in_pullback,
        "zones":           clean_zones,
        "fvgs":            fvgs,
        "lines":           all_lines,
        "anchor":          anchor_price,
        "anchor_idx":      int(anchor_idx),
        "anchor_high_idx": int(best_high_idx),
        "anchor_low_idx":  int(best_low_idx),
        "anchor_high_score": round(best_high_score, 4),
        "anchor_low_score":  round(best_low_score, 4),
        "atr":             atr,
        "sweeps":          recent_sweeps,
        "choch":           choch,
        "raw_swing_highs": [int(x) for x in raw_highs[-8:]],
        "raw_swing_lows":  [int(x) for x in raw_lows[-8:]]
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

        # ── INSTRUMENT PROFILE (pair-aware settings) ─────────────────────────
        profile  = get_instrument_profile(req.currency, current_price)
        decimals = profile['decimals']

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
        ms = analyze_market_structure(df, profile)
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
            # Only relevant if sweep happened in the last 20 candles
            candle_age = len(df) - 1 - last_sweep['sweep_idx']
            if candle_age <= 20:
                if last_sweep['type'] == "BEAR_SWEEP" and cycle == "BULLISH":
                    sweep_nearby = True
                    sweep_str    = f"🎯 Buy-side liq. swept at {last_sweep['level']:.{decimals}f} — stop hunt reversal zone."
                elif last_sweep['type'] == "BULL_SWEEP" and cycle == "BEARISH":
                    sweep_nearby = True
                    sweep_str    = f"🎯 Sell-side liq. swept at {last_sweep['level']:.{decimals}f} — stop hunt reversal zone."

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
                ob    = nearest_bull_ob
                score = score_ob_quality(ob, current_price, atr, rsi, cycle, sweep_nearby)
                entry_label = ob.get("entry_label", "ONLY BUYS")

                # Inside OB: price is between bottom - 0.5ATR and top + 0.1ATR
                inside_ob    = ob['bottom'] - (atr * 0.5) <= current_price <= ob['top'] + (atr * 0.1)
                # Approaching: within 2 ATR above the OB
                approaching  = ob['top'] < current_price <= ob['top'] + (atr * 2.0)

                if inside_ob:
                    signal     = "BUY"
                    target_ob  = ob
                    confidence = score if ema_trend_ok_bull else max(50, score - 15)
                    ema_str    = f"✅ Above 200 EMA ({ema_200:.{decimals}f})" if ema_trend_ok_bull else f"⚠️ Below 200 EMA — reduced conviction"
                    reasoning.append(f"🔥 KILLZONE: {entry_label} — Price inside 1H-OB ({ob['bottom']:.{decimals}f}–{ob['top']:.{decimals}f})")
                    reasoning.append(ema_str)
                    if rsi < 45:
                        reasoning.append(f"✅ RSI oversold confluence ({rsi:.1f})")
                    if sweep_nearby:
                        reasoning.append("🎯 Stop hunt sweep adds confluence")
                elif approaching:
                    reasoning.append(f"📍 {entry_label} — Approaching 1H-OB at {ob['bottom']:.{decimals}f}–{ob['top']:.{decimals}f}. Waiting for tap.")
                else:
                    dist = abs(current_price - ob['top'])
                    reasoning.append(f"📍 {entry_label} — Pulling back toward 1H-OB at {ob['bottom']:.{decimals}f} ({dist:.{decimals}f} away).")

            # ── BEARISH ENTRY ─────────────────────────────────────────────────
            elif cycle == "BEARISH" and nearest_bear_ob:
                ob    = nearest_bear_ob
                score = score_ob_quality(ob, current_price, atr, rsi, cycle, sweep_nearby)
                entry_label = ob.get("entry_label", "ONLY SELLS")

                inside_ob   = ob['bottom'] - (atr * 0.1) <= current_price <= ob['top'] + (atr * 0.5)
                approaching = ob['bottom'] - (atr * 2.0) <= current_price < ob['bottom']

                if inside_ob:
                    signal     = "SELL"
                    target_ob  = ob
                    confidence = score if ema_trend_ok_bear else max(50, score - 15)
                    ema_str    = f"✅ Below 200 EMA ({ema_200:.{decimals}f})" if ema_trend_ok_bear else f"⚠️ Above 200 EMA — reduced conviction"
                    reasoning.append(f"🔥 KILLZONE: {entry_label} — Price inside 1H-OB ({ob['bottom']:.{decimals}f}–{ob['top']:.{decimals}f})")
                    reasoning.append(ema_str)
                    if rsi > 55:
                        reasoning.append(f"✅ RSI overbought confluence ({rsi:.1f})")
                    if sweep_nearby:
                        reasoning.append("🎯 Stop hunt sweep adds confluence")
                elif approaching:
                    reasoning.append(f"📍 {entry_label} — Approaching 1H-OB at {ob['bottom']:.{decimals}f}–{ob['top']:.{decimals}f}. Waiting for tap.")
                else:
                    dist = abs(current_price - ob['bottom'])
                    reasoning.append(f"📍 {entry_label} — Rallying toward 1H-OB at {ob['top']:.{decimals}f} ({dist:.{decimals}f} away).")

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
                "sweeps":     sweeps,
            },
            "tradeSetup":  trade_setup,
            "dataSource":  data_source,
        }

    except Exception as e:
        logger.error(f"Analysis Crash: {e}", exc_info=True)
        return {"signal": "ERROR", "confidence": 0, "reasoning": [f"Engine error: {str(e)}"]}


@app.post("/api/debug")
async def debug_analysis(req: AnalysisRequest):
    """
    🔬 DIAGNOSTIC ENDPOINT — verifies the engine is working correctly.
    Returns a full human-readable breakdown of every decision the engine made:
    - Which candle was chosen as anchor and WHY (score)
    - Every swing pivot found (so you can verify against the chart)
    - Every BOS level and which candles triggered it
    - Every OB: which candle it came from, top/bottom, mitigated?
    - Current phase and what the engine is waiting for
    - ATR, swing_order, profile used
    Call: POST /api/debug  (same body as /api/analyze)
    """
    if req.candles and len(req.candles) > 50:
        df = process_live_candles(req.candles)
    else:
        df = MARKET_MEMORY["df"]

    if df is None or df.empty:
        return {"error": "No data available"}

    try:
        csv_last_price = safe_float(df['Close'].iloc[-1])
        current_price  = req.current_price if req.current_price > 0 else csv_last_price
        profile        = get_instrument_profile(req.currency, current_price)
        decimals       = profile['decimals']
        atr            = calculate_atr(df, 14)
        swing_order    = adaptive_swing_order(df, atr)

        highs  = df['High'].values
        lows   = df['Low'].values
        closes = df['Close'].values
        dates  = df['Date'].values if 'Date' in df.columns else df.index.values

        raw_highs = argrelextrema(highs, np.greater, order=swing_order)[0]
        raw_lows  = argrelextrema(lows,  np.less,    order=swing_order)[0]

        ANCHOR_LOOKBACK = min(len(closes), 600)
        search_highs = raw_highs[raw_highs >= len(closes) - ANCHOR_LOOKBACK]
        search_lows  = raw_lows[raw_lows   >= len(closes) - ANCHOR_LOOKBACK]

        # Score all swing highs
        high_scores = []
        for sh in search_highs:
            subsequent_low = float(np.min(lows[sh:])) if sh < len(lows) - 1 else float(highs[sh])
            drop = float(highs[sh]) - subsequent_low
            high_scores.append({
                "candle_idx":    int(sh),
                "price":         round(float(highs[sh]), decimals),
                "date":          str(dates[sh]),
                "drop_after":    round(drop, decimals),
                "is_chosen":     False
            })
        high_scores.sort(key=lambda x: x["drop_after"], reverse=True)

        # Score all swing lows
        low_scores = []
        for sl in search_lows:
            subsequent_high = float(np.max(highs[sl:])) if sl < len(highs) - 1 else float(lows[sl])
            rally = subsequent_high - float(lows[sl])
            low_scores.append({
                "candle_idx":   int(sl),
                "price":        round(float(lows[sl]), decimals),
                "date":         str(dates[sl]),
                "rally_after":  round(rally, decimals),
                "is_chosen":    False
            })
        low_scores.sort(key=lambda x: x["rally_after"], reverse=True)

        # Run full analysis to get results
        ms = analyze_market_structure(df, profile)

        # Mark chosen anchor
        for h in high_scores:
            if h["candle_idx"] == ms.get("anchor_high_idx", -1):
                h["is_chosen"] = True
        for l in low_scores:
            if l["candle_idx"] == ms.get("anchor_low_idx", -1):
                l["is_chosen"] = True

        # Build BOS summary
        bos_summary = []
        for b in ms['lines']:
            if "BOS" in b.get("type", ""):
                bos_summary.append({
                    "label":       b["type"],
                    "level":       round(b["level"], decimals),
                    "start_time":  b["start_time"],
                    "end_time":    b["end_time"],
                })

        # Build OB summary
        ob_summary = []
        for z in ms['zones']:
            ob_summary.append({
                "type":        z["type"],
                "label":       z.get("label", ""),
                "entry_label": z.get("entry_label", ""),
                "top":         round(z["top"], decimals),
                "bottom":      round(z["bottom"], decimals),
                "height_atr":  round((z["top"] - z["bottom"]) / atr, 2),
                "candle_idx":  z.get("candle_idx", -1),
                "mitigated":   z.get("is_mitigated", False),
            })

        return {
            "✅ ENGINE VERSION":    "AuraBrain SMC v11",
            "📊 INSTRUMENT":       req.currency,
            "💰 CURRENT PRICE":    round(current_price, decimals),
            "📐 PROFILE":          profile,
            "📏 ATR (14)":         round(atr, decimals),
            "🔍 SWING ORDER":      swing_order,
            "📈 TOTAL CANDLES":    len(df),
            "─── ANCHOR ───": "─────────────────────────────────────────",
            "🎯 CYCLE":            ms['cycle'],
            "🎯 ANCHOR PRICE":     round(ms['anchor'], decimals),
            "🎯 ANCHOR CANDLE":    int(anchor_idx := ms.get('anchor_idx', 0)),
            "─── SWING HIGHS (ranked by drop) ───": "────────────────",
            "swing_highs_scored":  high_scores[:6],
            "─── SWING LOWS (ranked by rally) ───": "────────────────",
            "swing_lows_scored":   low_scores[:6],
            "─── STRUCTURE ───": "──────────────────────────────────────",
            "📊 PHASE":            f"Level {ms['level']} ({'PULLBACK' if ms['in_pullback'] else 'EXPANSION'})",
            "🔵 BOS LINES":        bos_summary,
            "🟥 ORDER BLOCKS":     ob_summary,
            "⚠️  CHoCH":           ms['choch'],
            "🎯 SWEEPS":           ms['sweeps'],
            "─── VERDICT ───": "──────────────────────────────────────",
            "🧭 BIAS":             f"{ms['cycle']} CYCLE",
            "📍 WAITING FOR":      f"Price to tap {'Bullish OB' if ms['cycle'] == 'BULLISH' else 'Bearish OB'}" if ms['in_pullback'] else "Expansion to complete before pullback",
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
    except Exception:
        raise HTTPException(status_code=503, detail="Node proxy unavailable")