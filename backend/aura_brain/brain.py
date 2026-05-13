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

# ─────────────────────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────────────────────
from ta.trend import EMAIndicator, SMAIndicator
from ta.momentum import RSIIndicator, StochasticOscillator

import joblib

CSV_FILENAME = "1h.csv"
NODE_URL = "http://127.0.0.1:10000"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}

# Signal lock — prevents signal flipping on every refresh
SIGNAL_LOCK = {}  

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
# IMPROVED PEAK DETECTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def adaptive_swing_order(df: pd.DataFrame, atr: float) -> int:
    avg_range = float((df['High'] - df['Low']).tail(50).mean())
    ratio = avg_range / atr if atr > 0 else 1.0
    order = int(np.clip(ratio * 10, 5, 20))
    return order

# Optional: Simple ZigZag (uncomment if you want to try instead of argrelextrema)
"""
def zigzag(df: pd.DataFrame, depth_pct: float = 0.005) -> Dict:
    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values
    pivot_highs = []
    pivot_lows = []
    last_pivot_idx = 0
    last_pivot_price = closes[0]
    last_pivot_type = None

    for i in range(1, len(closes)):
        if highs[i] > last_pivot_price * (1 + depth_pct):
            if last_pivot_type == 'high':
                pivot_highs.pop()
            pivot_highs.append(i)
            last_pivot_idx = i
            last_pivot_price = highs[i]
            last_pivot_type = 'high'
        elif lows[i] < last_pivot_price * (1 - depth_pct):
            if last_pivot_type == 'low':
                pivot_lows.pop()
            pivot_lows.append(i)
            last_pivot_idx = i
            last_pivot_price = lows[i]
            last_pivot_type = 'low'

    return {'highs': np.array(pivot_highs), 'lows': np.array(pivot_lows)}
"""

# ─────────────────────────────────────────────────────────────────────────────
# 🟢 IMPROVED: ALPHAPEAK (BEAST MARKET SENTIMENT) ENGINE
# ─────────────────────────────────────────────────────────────────────────────
def analyze_alpha_peak(df: pd.DataFrame) -> Dict:
    """
    Improved AlphaPeak with adaptive order + prominence filter
    """
    try:
        if len(df) < 100:
            return {"signal": "NONE", "reason": "Not enough data for AlphaPeak"}

        closes = df['Close']
        highs = df['High'].values
        lows = df['Low'].values
        
        atr = calculate_atr(df, 14)
        base_order = adaptive_swing_order(df, atr)
        order = base_order * 3          # Scale up for macro peaks (15–60 range)
        min_prominence = atr * 0.4      # Ignore very small peaks

        # ─── Peak Detection ───
        # You can switch to zigzag by uncommenting below and using zz['highs']/zz['lows']
        # zz = zigzag(df, depth_pct=0.005)
        # recent_peak_highs = zz['highs']
        # recent_peak_lows = zz['lows']

        recent_peak_highs = argrelextrema(highs, np.greater, order=order)[0]
        recent_peak_lows  = argrelextrema(lows,  np.less,    order=order)[0]

        # Simple prominence filter (difference from neighbors)
        filtered_highs = []
        for idx in recent_peak_highs:
            if idx > order and idx < len(highs) - order:
                left_min  = np.min(highs[idx-order:idx])
                right_min = np.min(highs[idx+1:idx+order+1])
                if highs[idx] - max(left_min, right_min) >= min_prominence:
                    filtered_highs.append(idx)

        filtered_lows = []
        for idx in recent_peak_lows:
            if idx > order and idx < len(lows) - order:
                left_max  = np.max(lows[idx-order:idx])
                right_max = np.max(lows[idx+1:idx+order+1])
                if min(left_max, right_max) - lows[idx] >= min_prominence:
                    filtered_lows.append(idx)

        recent_peak_highs = np.array(filtered_highs)
        recent_peak_lows  = np.array(filtered_lows)

        # Debug logging — remove or comment out in production
        logger.info(f"AlphaPeak: order={order}, prominence={min_prominence:.5f}")
        logger.info(f"AlphaPeak detected highs: {recent_peak_highs.tolist()}")
        logger.info(f"AlphaPeak detected lows : {recent_peak_lows.tolist()}")

        last_index = len(df) - 1
        recent_bull_peak = any((last_index - idx) <= 5 for idx in recent_peak_lows)
        recent_bear_peak = any((last_index - idx) <= 5 for idx in recent_peak_highs)

        # 1. Moving Average Trend Filter
        sma_10 = SMAIndicator(close=closes, window=10).sma_indicator().iloc[-1]
        sma_15 = SMAIndicator(close=closes, window=15).sma_indicator().iloc[-1]
        
        # 2. Stochastic
        stoch = StochasticOscillator(high=df['High'], low=df['Low'], close=closes, window=7, smooth_window=3)
        current_stoch = stoch.stoch().iloc[-1]

        # 3. Rules
        if recent_bull_peak and sma_10 > sma_15 and current_stoch < 30.0:
            return {"signal": "BUY", "reason": "AlphaPeak Buy Vector (Low Peak + Trend Up + Oversold)"}
        elif recent_bear_peak and sma_10 < sma_15 and current_stoch > 70.0:
            return {"signal": "SELL", "reason": "AlphaPeak Sell Vector (High Peak + Trend Down + Overbought)"}
            
        return {"signal": "NONE", "reason": "No AlphaPeak confluence"}
        
    except Exception as e:
        logger.error(f"AlphaPeak analysis failed: {e}")
        return {"signal": "NONE", "reason": "AlphaPeak Engine Error"}

# ─────────────────────────────────────────────────────────────────────────────
# CORE ENGINE  — "Aura MMM v2.1 → v2.3 (improved peaks)"
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

def detect_order_blocks(df: pd.DataFrame, atr: float, bias: str) -> List[Dict]:
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    
    obs = []
    # look at last 100 candles only — recent OBs matter most
    start = max(0, len(closes) - 100)
    
    for i in range(start + 2, len(closes) - 1):
        body_size = abs(closes[i] - closes[i-1])
        if body_size < atr * 0.3:
            continue  # ignore small candles, not a real OB
            
        if bias == 'BULLISH':
            # Bullish OB: last bearish candle before a strong bullish impulse
            is_bearish_candle = closes[i-1] < closes[i-2]  # candle i-1 closed down
            strong_up_move    = closes[i] > highs[i-1] and (closes[i] - closes[i-1]) > atr * 0.8
            if is_bearish_candle and strong_up_move:
                obs.append({
                    'type':       'BULL_OB',
                    'top':        float(highs[i-1]),
                    'bottom':     float(lows[i-1]),
                    'time':       int(dates[i-1]),
                    'end_time':   int(dates[-1]),
                    'label':      'Bull OB',
                    'mitigated':  float(lows[-1]) > float(lows[i-1])  # price hasn't returned yet
                })
        else:
            # Bearish OB: last bullish candle before a strong bearish impulse
            is_bullish_candle = closes[i-1] > closes[i-2]
            strong_down_move  = closes[i] < lows[i-1] and (closes[i-1] - closes[i]) > atr * 0.8
            if is_bullish_candle and strong_down_move:
                obs.append({
                    'type':       'BEAR_OB',
                    'top':        float(highs[i-1]),
                    'bottom':     float(lows[i-1]),
                    'time':       int(dates[i-1]),
                    'end_time':   int(dates[-1]),
                    'label':      'Bear OB',
                    'mitigated':  float(highs[-1]) < float(highs[i-1])
                })
    
    # return only last 3 unmitigated OBs — most recent ones only
    unmitigated = [ob for ob in obs if not ob['mitigated']]
    return unmitigated[-3:] if unmitigated else obs[-2:]


def detect_fvgs(df: pd.DataFrame, atr: float, bias: str) -> List[Dict]:
    highs = df['High'].values
    lows  = df['Low'].values
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    
    fvgs = []
    start = max(0, len(highs) - 100)
    
    for i in range(start + 1, len(highs) - 1):
        if bias == 'BULLISH':
            gap = lows[i+1] - highs[i-1]
            if gap > atr * 0.3:
                filled = any(lows[j] <= highs[i-1] for j in range(i+2, len(lows)))
                if not filled:
                    fvgs.append({
                        'type':     'BULL_FVG',
                        'top':      float(lows[i+1]),
                        'bottom':   float(highs[i-1]),
                        'time':     int(dates[i]),
                        'end_time': int(dates[-1]),
                        'label':    'FVG',
                        'size':     float(gap)
                    })
        else:
            gap = lows[i-1] - highs[i+1]
            if gap > atr * 0.3:
                filled = any(highs[j] >= lows[i-1] for j in range(i+2, len(highs)))
                if not filled:
                    fvgs.append({
                        'type':     'BEAR_FVG',
                        'top':      float(lows[i-1]),
                        'bottom':   float(highs[i+1]),
                        'time':     int(dates[i]),
                        'end_time': int(dates[-1]),
                        'label':    'FVG',
                        'size':     float(gap)
                    })
    
    return fvgs[-3:]


def get_session_levels(df: pd.DataFrame) -> Dict:
    if len(df) < 24:
        return {}
    
    try:
        prev_day    = df.iloc[-48:-24]
        weekly_open = df.iloc[-120] if len(df) >= 120 else df.iloc[0]
        
        prev_day_high  = float(prev_day['High'].max())
        prev_day_low   = float(prev_day['Low'].min())
        weekly_open_px = float(weekly_open['Open']) if 'Open' in df.columns else float(weekly_open['Close'])
        current_price  = float(df['Close'].iloc[-1])
        
        return {
            'prev_day_high':  prev_day_high,
            'prev_day_low':   prev_day_low,
            'weekly_open':    weekly_open_px,
            'price_vs_pdh':   current_price > prev_day_high,
            'price_vs_pdl':   current_price < prev_day_low,
            'price_vs_wopen': current_price > weekly_open_px,
        }
    except Exception as e:
        logger.error(f"Session levels error: {e}")
        return {}


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

def detect_mmm_consolidations(df: pd.DataFrame, anchor_idx: int, cycle: str, atr: float, ema_50: np.ndarray, ema_200: np.ndarray) -> List[Dict]:
    # ─────────────────────────────────────────────────────────────────────────────
    # (unchanged from your original — keeping as is)
    # ─────────────────────────────────────────────────────────────────────────────
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    
    boxes = []
    min_box_length = 5
    ema_proximity = atr * 1.0 
    
    if cycle.startswith("BULLISH"):
        cross_idx = -1
        for i in range(anchor_idx, len(closes)):
            if ema_50[i] > ema_200[i] and closes[i] > ema_200[i]:
                cross_idx = i
                break
                
        if cross_idx == -1: return boxes 

        current_idx = cross_idx 

        for box_num in range(1, 3):
            if current_idx >= len(closes) - min_box_length: break

            peak_val = highs[current_idx]
            peak_idx = current_idx
            pullback_idx = -1

            for i in range(current_idx, len(closes)):
                if highs[i] > peak_val:
                    peak_val = highs[i]
                    peak_idx = i

                if peak_val - lows[i] >= atr:
                    if lows[i] <= ema_50[i] + ema_proximity:
                        if closes[i] > ema_200[i] - (atr * 0.5):
                            pullback_idx = i
                            break

            if pullback_idx == -1: break 

            box_top = peak_val
            box_bottom = lows[pullback_idx]
            breakout_idx = len(closes) - 1
            breakout_dir = "none"

            for i in range(pullback_idx, len(closes)):
                if lows[i] < box_bottom: box_bottom = lows[i]

                if closes[i] > box_top:
                    breakout_idx = i
                    breakout_dir = "up"
                    break
                elif closes[i] < box_bottom - atr: 
                    breakout_idx = i
                    breakout_dir = "down"
                    break

            if breakout_idx - peak_idx >= min_box_length:
                pullback_top    = float(ema_50[breakout_idx] + atr * 0.5)
                pullback_bottom = float(ema_50[breakout_idx] - atr * 0.5)
                boxes.append({
                    "time": int(dates[peak_idx]),
                    "end_time": int(dates[breakout_idx]),
                    "top": float(box_top),
                    "bottom": float(box_bottom),
                    "type": "BULL_CONS",
                    "label": f"CONS {box_num} (Prep L{box_num + 1})",
                    "breakout_dir": breakout_dir,
                    "pullback_zone_top": pullback_top,
                    "pullback_zone_bottom": pullback_bottom,
                    "pullback_label": f"L{box_num + 1} Entry Zone"
                })

                if breakout_dir != "up": break 
                current_idx = breakout_idx
            else:
                if breakout_dir == "up":
                    current_idx = breakout_idx
                    continue
                break

    elif cycle.startswith("BEARISH"):
        cross_idx = -1
        for i in range(anchor_idx, len(closes)):
            if ema_50[i] < ema_200[i] and closes[i] < ema_200[i]:
                cross_idx = i
                break
                
        if cross_idx == -1: return boxes 

        current_idx = cross_idx

        for box_num in range(1, 3):
            if current_idx >= len(closes) - min_box_length: break

            trough_val = lows[current_idx]
            trough_idx = current_idx
            pullback_idx = -1

            for i in range(current_idx, len(closes)):
                if lows[i] < trough_val:
                    trough_val = lows[i]
                    trough_idx = i

                if highs[i] - trough_val >= atr:
                    if highs[i] >= ema_50[i] - ema_proximity:
                        if closes[i] < ema_200[i] + (atr * 0.5):
                            pullback_idx = i
                            break

            if pullback_idx == -1: break

            box_bottom = trough_val
            box_top = highs[pullback_idx]
            breakout_idx = len(closes) - 1
            breakout_dir = "none"

            for i in range(pullback_idx, len(closes)):
                if highs[i] > box_top: box_top = highs[i]

                if closes[i] < box_bottom:
                    breakout_idx = i
                    breakout_dir = "down"
                    break
                elif closes[i] > box_top + atr:
                    breakout_idx = i
                    breakout_dir = "up"
                    break

            if breakout_idx - trough_idx >= min_box_length:
                pullback_top    = float(ema_50[breakout_idx] + atr * 0.5)
                pullback_bottom = float(ema_50[breakout_idx] - atr * 0.5)
                boxes.append({
                    "time": int(dates[trough_idx]),
                    "end_time": int(dates[breakout_idx]),
                    "top": float(box_top),
                    "bottom": float(box_bottom),
                    "type": "BEAR_CONS",
                    "label": f"CONS {box_num} (Prep L{box_num + 1})",
                    "breakout_dir": breakout_dir,
                    "pullback_zone_top": pullback_top,
                    "pullback_zone_bottom": pullback_bottom,
                    "pullback_label": f"L{box_num + 1} Entry Zone"
                })

                if breakout_dir != "down": break
                current_idx = breakout_idx
            else:
                if breakout_dir == "down":
                    current_idx = breakout_idx
                    continue
                break

    return boxes

def analyze_market_structure(df: pd.DataFrame, profile: Dict) -> Dict:
    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values

    atr = calculate_atr(df, 14)
    if atr == 0: atr = float(df['Close'].mean()) * 0.001

    swing_order = adaptive_swing_order(df, atr)
    min_prominence = atr * 0.3  # NEW: filter small swings

    raw_highs_idx = argrelextrema(highs, np.greater, order=swing_order)[0]
    raw_lows_idx  = argrelextrema(lows,  np.less,    order=swing_order)[0]

    # Prominence filter for core swings
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

    raw_highs = np.array(filtered_highs)
    raw_lows  = np.array(filtered_lows)

    # Debug log for core swings
    logger.info(f"Core MMM swings: order={swing_order}, prominence={min_prominence:.5f}")
    logger.info(f"Detected swing highs: {raw_highs.tolist()}")
    logger.info(f"Detected swing lows : {raw_lows.tolist()}")

    ema_200_series = df['Close'].ewm(span=200, adjust=False).mean()
    ema_50_series  = df['Close'].ewm(span=50, adjust=False).mean()
    ema_50_array   = ema_50_series.values
    ema_200_array  = ema_200_series.values

    is_bullish = ema_50_array[-1] > ema_200_array[-1]
    cross_idx = 0
    for i in range(len(closes) - 2, 0, -1):
        if is_bullish and ema_50_array[i] <= ema_200_array[i]:
            cross_idx = i
            break
        elif not is_bullish and ema_50_array[i] >= ema_200_array[i]:
            cross_idx = i
            break

    search_start = max(0, cross_idx - 150)
    search_end   = min(len(closes), cross_idx + 50)

    if is_bullish:
        use_bearish = False
        best_low_idx = search_start + int(np.argmin(lows[search_start:search_end]))
    else:
        use_bearish = True
        best_high_idx = search_start + int(np.argmax(highs[search_start:search_end]))

    current_price   = float(closes[-1])
    current_ema_200 = float(ema_200_series.iloc[-1])
    current_ema_50  = float(ema_50_series.iloc[-1])

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
    consolidation_boxes = detect_mmm_consolidations(df, anchor_idx, cycle, atr, ema_50_array, ema_200_array)
     if consolidation_boxes:
        consolidation_boxes = consolidation_boxes[-2:]
    total_boxes = len(consolidation_boxes)
    in_pullback = False
    
    if total_boxes == 0:
        display_level = 1
        phase_str = "LEVEL 1 (Initial Breakaway - No Trade Zone)"
    else:
        last_box = consolidation_boxes[-1]
        breakout_dir = last_box.get('breakout_dir', 'none')
        
        if breakout_dir == 'none' or last_box['end_time'] == int(dates[-1]):
            in_pullback = True
            target_lvl = total_boxes + 1
            phase_str = f"PULLBACK (Prep for Level {target_lvl} Trade)"
            display_level = total_boxes
        elif (cycle.startswith("BEARISH") and breakout_dir == "up") or \
             (cycle.startswith("BULLISH") and breakout_dir == "down"):
            in_pullback = False
            display_level = total_boxes
            phase_str = "CYCLE FAILED (Reversal Detected)"
        else:
            in_pullback = False
            if total_boxes == 2:
                display_level = 3
                phase_str = "LEVEL 3 EXHAUSTION (Blowoff Active)"
            else:
                display_level = total_boxes + 1
                phase_str = f"LEVEL {display_level} (Pushing Active)"

    all_lines = [{
        "level": anchor_price,
        "start_time": int(dates[anchor_idx]),
        "end_time": int(dates[-1]),
        "type": pattern_name,
        "color": anchor_color,
        "is_choch": False
    }]

    return {
        "cycle":           cycle,
        "level":           display_level,  
        "phase_str":       phase_str,
        "in_pullback":     in_pullback,
        "lines":           all_lines,
        "anchor":          anchor_price,
        "anchor_idx":      int(anchor_idx),
        "atr":             atr,
        "sweeps":          sweeps,
        "consolidation_boxes": consolidation_boxes
    }

def score_mmm_setup(current_price, ema_50, ema_200, rsi, level, in_pullback, cycle, sweep_nearby, atr, phase_str, ob_present=False, fvg_present=False, session_aligned=False, htf_aligned=False) -> int:
    if "FAILED" in phase_str: return 0
    if not in_pullback: return 0  # never score outside pullback phase

    score = 0

    # Layer 1 — HTF bias (20pts)
    if htf_aligned: score += 20

    # Layer 2 — MMM phase (15pts)
    if level in [1, 2]: score += 15
    elif level >= 3: score -= 10

    # Layer 3 — Price at liquidity zone (15pts)
    dist = abs(current_price - ema_50)
    if dist <= atr * 0.5: score += 15
    elif dist <= atr * 1.0: score += 8

    # Layer 4 — ICT triggers (25pts)
    if ob_present: score += 15
    if fvg_present: score += 10

    # Layer 5 — Liquidity sweep nearby (10pts)
    if sweep_nearby: score += 10

    # Layer 6 — Session timing (5pts)
    if session_aligned: score += 5

    # RSI confirmation
    if cycle.startswith("BULLISH") and rsi < 50: score += 5
    elif cycle.startswith("BEARISH") and rsi > 50: score += 5

    return min(max(score, 0), 99)

def calculate_trade_levels(current_price: float, signal: str,
                            atr: float, decimals: int, ema_50: float) -> Optional[Dict]:
    try:
        entry = current_price
        if signal == "BUY":
            stop_loss = min(entry - (atr * 1.5), ema_50 - (atr * 0.5))
            risk = abs(entry - stop_loss)
            take_profit = entry + (risk * 2.0)
        elif signal == "SELL":
            stop_loss = max(entry + (atr * 1.5), ema_50 + (atr * 0.5))
            risk = abs(entry - stop_loss)
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
# API ENDPOINT (unchanged except using improved functions)
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

        news_val    = 0
        news_string = "No recent impactful news."
        if req.news_data:
            actual   = safe_float(req.news_data.get('actual', 0))
            forecast = safe_float(req.news_data.get('forecast', 0))
            event    = req.news_data.get('event', 'News Event')
            if actual > forecast:
                news_val    = -1   
                news_string = f"📰 {event}: Beat forecast ({actual} vs {forecast}). USD bullish."
            elif actual < forecast:
                news_val    = 1
                news_string = f"📰 {event}: Missed forecast ({actual} vs {forecast}). USD bearish."

        ms = analyze_market_structure(df, profile)
        cycle         = ms['cycle']
        current_level = ms['level']
        phase_str     = ms['phase_str']
        in_pullback   = ms['in_pullback']
        lines         = ms['lines']
        sweeps        = ms['sweeps']
        boxes         = ms.get('consolidation_boxes', [])

        # ICT triggers
        bias_str = 'BULLISH' if cycle.startswith('BULLISH') else 'BEARISH'
        order_blocks = detect_order_blocks(df, ms['atr'], bias_str)
        fvgs         = detect_fvgs(df, ms['atr'], bias_str)
        session_lvls = get_session_levels(df)

        ob_present = any(
            (ob['bottom'] - ms['atr']) <= current_price <= (ob['top'] + ms['atr'])
            for ob in order_blocks
        )
        fvg_present = any(
            (fvg['bottom'] - ms['atr']) <= current_price <= (fvg['top'] + ms['atr'])
            for fvg in fvgs
        )

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

        from datetime import datetime, timezone
        current_utc_hour = datetime.now(timezone.utc).hour
        session_aligned = (8 <= current_utc_hour < 17) or (13 <= current_utc_hour < 22)
        
        alpha_data = analyze_alpha_peak(df)
        alpha_signal = alpha_data["signal"]
        alpha_reason = alpha_data["reason"]

        sweep_nearby = False
        sweep_str = ""
        if sweeps:
            last_sweep = sweeps[-1]
            candle_age = len(df) - 1 - last_sweep['sweep_idx']
            if candle_age <= 15:
                sweep_nearby = True
                sweep_str = f"🎯 Stop Hunt / Pin detected at {last_sweep['level']:.{decimals}f}."

        signal     = "NEUTRAL"
        confidence = 0

        reasoning = [
            f"🧭 Market Maker Bias: {cycle}",
            f"📊 Phase: {phase_str}",
            f"📈 Price vs Macro Trend: {ema_bias}",
            news_string,
        ]

        if sweep_str: reasoning.append(sweep_str)

        dist = abs(current_price - ema_50)
        
        if "FAILED" in phase_str:
            reasoning.append("⚠️ Trend structure broken. Awaiting 200 EMA crossover to reset Anchor.")
        elif "No Trade Zone" in phase_str:
            reasoning.append("🚫 Level 1 Breakaway active. Waiting for first consolidation pullback to trade.")
        elif "EXHAUSTION" in phase_str and not in_pullback:
            reasoning.append("⏳ Level 3 Exhaustion. Anticipating macro reversal or reset.")
        elif not in_pullback:
            reasoning.append("🔄 Expansion phase active. Waiting for pullback to 50 EMA before entering.")
        else:
            if cycle.startswith("BULLISH"):
                if current_price >= ema_50 * 0.998 and dist <= (atr * 1.5):
                    signal = "BUY"
                    confidence = score_mmm_setup(current_price, ema_50, ema_200, rsi, current_level, in_pullback, cycle, sweep_nearby, atr, phase_str, ob_present, fvg_present, session_aligned, htf_aligned)
                    reasoning.append(f"🔥 KILLZONE: Entering on 50 EMA for {phase_str.split('(')[-1].strip(')')}.")
                else:
                    reasoning.append(f"📍 Pulling back. Waiting for tap on 50 EMA ({ema_50:.{decimals}f}).")
            else:
                if current_price <= ema_50 * 1.002 and dist <= (atr * 1.5):
                    signal = "SELL"
                    confidence = score_mmm_setup(current_price, ema_50, ema_200, rsi, current_level, in_pullback, cycle, sweep_nearby, atr, phase_str, ob_present, fvg_present, session_aligned, htf_aligned)
                    reasoning.append(f"🔥 KILLZONE: Entering on 50 EMA for {phase_str.split('(')[-1].strip(')')}.")
                else:
                    reasoning.append(f"📍 Pulling back. Waiting for tap on 50 EMA ({ema_50:.{decimals}f}).")

        if signal != "NEUTRAL":
            if alpha_signal == signal:
                confidence = min(confidence + 15, 99)
                reasoning.append(f"🐺 BEAST SENTIMENT CONFLUENCE: {alpha_reason} (+15% Confidence)")
            elif alpha_signal != "NONE":
                confidence = max(confidence - 20, 10)
                reasoning.append(f"⚠️ CONFLICT: AlphaPeak suggests {alpha_signal}. Reducing confidence.")

        if signal != "NEUTRAL" and ML_MODEL:
            try:
                features = pd.DataFrame([{
                    'type':          1 if signal == "BUY" else 0,
                    'fvg_size_pips':  0.0,
                    'rsi_at_entry':   rsi,
                    'atr_at_entry':   atr,
                    'momentum_ratio': 1.0, 
                    'news_bias':      news_val
                }])
                prob = ML_MODEL.predict_proba(features)[0][1]
                ml_conf = int(prob * 100)
                confidence = int((confidence * 0.7) + (ml_conf * 0.3))
                reasoning.append(f"🧠 ML Prediction: {ml_conf}% win probability.")
            except Exception as ml_e:
                logger.warning(f"ML inference failed: {ml_e}")

        trade_setup = None
        symbol_key = req.currency
        existing_lock = SIGNAL_LOCK.get(symbol_key)
        if existing_lock:
            sl_breached = (existing_lock['signal'] == 'BUY' and current_price < existing_lock['sl']) or \
                          (existing_lock['signal'] == 'SELL' and current_price > existing_lock['sl'])
            bias_flipped = existing_lock['signal'] == 'BUY' and not cycle.startswith('BULLISH') or \
                           existing_lock['signal'] == 'SELL' and not cycle.startswith('BEARISH')
            if sl_breached or bias_flipped:
                del SIGNAL_LOCK[symbol_key]
            else:
                signal = existing_lock['signal']
                confidence = existing_lock['confidence']

        if signal in ("BUY", "SELL") and confidence >= 65:
            trade_setup = calculate_trade_levels(current_price, signal, atr, decimals, ema_50)
            if trade_setup and symbol_key not in SIGNAL_LOCK:
                SIGNAL_LOCK[symbol_key] = {
                    'signal': signal,
                    'confidence': confidence,
                    'sl': trade_setup['stop_loss']
                }
            if trade_setup:
                reasoning.append(
                    f"📐 Setup: Entry {trade_setup['entry']} | SL {trade_setup['stop_loss']} | TP {trade_setup['take_profit']} | RR 1:{trade_setup['risk_reward']}"
                )
              

        return {
            "signal":     signal,
            "confidence": int(confidence),
            "trend":      cycle,
            "pattern":    "MMM Level Pullback to EMA",
            "reasoning":  reasoning,
            "keyLevels": {
                "resistance": round(current_price + (atr * 2), decimals),
                "support":    round(current_price - (atr * 2), decimals),
                "ema200":     round(ema_200, decimals),
                "ema50":      round(ema_50, decimals),
            },

            "visuals": {
                "smc_zones":    boxes,
                "bos_lines":    lines,
                "fvgs":         fvgs,
                "sweeps":       sweeps,
                "order_blocks": order_blocks,
            },
            "mtf_confluence": [
                {"tf": "4H",  "bias": "BULLISH" if htf_aligned and cycle.startswith("BULLISH") else "BEARISH" if htf_aligned else "NEUTRAL", "strength": 85 if htf_aligned else 40},
                {"tf": "1H", "bias": bias_str, "strength": 75 if bias_str == "BULLISH" else 65 if bias_str == "BEARISH" else 20},
                {"tf": "OB",  "bias": bias_str if len(order_blocks) > 0 else "NEUTRAL",  "strength": 80 if len(order_blocks) > 0 else 30},
                {"tf": "FVG", "bias": bias_str if len(fvgs) > 0 else "NEUTRAL", "strength": 75 if len(fvgs) > 0 else 25},
            ],

            "tradeSetup":  trade_setup,
            "dataSource":  data_source,
        }

    except Exception as e:
        logger.error(f"Analysis Crash: {e}", exc_info=True)
        return {"signal": "ERROR", "confidence": 0, "reasoning": [f"Engine error: {str(e)}"]}

# ─────────────────────────────────────────────────────────────────────────────
# DEBUG ENDPOINT (enhanced with peak info)
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
        alpha = analyze_alpha_peak(df)

        return {
            "✅ ENGINE VERSION":    "AuraBrain MMM v2.3 (improved peaks 2026)",
            "📊 INSTRUMENT":       req.currency,
            "💰 CURRENT PRICE":    round(current_price, profile['decimals']),
            "─── STRUCTURE ───": "──────────────────────────────────────",
            "🎯 CYCLE":            ms['cycle'],
            "📊 PHASE":            ms['phase_str'],
            "📦 CONSOLIDATIONS":   ms.get('consolidation_boxes', []),
            "─── PEAK INFO ───": "──────────────────────────────────────",
            "AlphaPeak Signal":   alpha['signal'],
            "AlphaPeak Reason":   alpha['reason'],
            "Core Swings Highs":  ms.get('raw_highs', []),   # won't exist — for future
            "Core Swings Lows":   ms.get('raw_lows', []),    # won't exist — for future
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