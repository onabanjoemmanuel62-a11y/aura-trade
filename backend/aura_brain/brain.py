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
# CORE ENGINE  — "AURA MMM v1.1" (VISUALS UPGRADE)
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

def adaptive_swing_order(df: pd.DataFrame, atr: float) -> int:
    avg_range = float((df['High'] - df['Low']).tail(50).mean())
    ratio = avg_range / atr if atr > 0 else 1.0
    order = int(np.clip(ratio * 10, 5, 20))
    return order

def detect_liquidity_sweeps(df: pd.DataFrame, swing_highs: np.ndarray, swing_lows: np.ndarray, atr: float) -> List[Dict]:
    """Detects Stop Hunts (Pins/Spikes) crucial to MMM reversals."""
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
                    "type":      "BULL_SWEEP", # Kept identical for frontend compatibility
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
                    "type":      "BEAR_SWEEP", # Kept identical for frontend compatibility
                    "level":     float(sl_price),
                    "sweep_idx": int(i),
                    "time":      int(dates[i]),
                    "wick_size": float(wick_below),
                })
                break
    sweeps.sort(key=lambda x: x['sweep_idx'])
    return sweeps[-5:]

# 🟢 UPDATED: MMM CONSOLIDATION BOX DETECTOR (Sequential Stepping)
def detect_mmm_consolidations(df: pd.DataFrame, anchor_idx: int, cycle: str, raw_highs: np.ndarray, raw_lows: np.ndarray, atr: float) -> List[Dict]:
    """Finds the consolidation boxes (pullbacks) sequentially to prevent overlapping."""
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    boxes  = []
    
    level_count = 1
    search_start_idx = anchor_idx
    
    if cycle.startswith("BEARISH"):
        while level_count <= 3:
            # 1. Find the first major Low after the current search point (The initial drop)
            valid_lows = raw_lows[raw_lows > search_start_idx]
            if len(valid_lows) == 0: break
            sl_idx = valid_lows[0]
            box_bottom = float(lows[sl_idx])
            
            # 2. Track the pullback High until price breaks below the box bottom
            breakout_idx = len(closes) - 1
            box_top = float(highs[sl_idx]) # Initialize at the low

            for j in range(sl_idx + 1, len(closes)):
                # If price pulls back higher, stretch the top of the box
                if highs[j] > box_top:
                    box_top = float(highs[j])
                # If price drops below the bottom, the consolidation is over
                if closes[j] < box_bottom:
                    breakout_idx = j
                    break
            
            # Only draw if the box isn't pure micro-noise
            if (box_top - box_bottom) >= (atr * 0.3):
                boxes.append({
                    "time": int(dates[sl_idx]),
                    "end_time": int(dates[breakout_idx]),
                    "top": box_top,
                    "bottom": box_bottom,
                    "type": "BEAR_CONS",
                    "label": f"LEVEL {level_count}",
                })
                level_count += 1
            
            # Move the search index forward to the breakout candle so boxes can't overlap
            search_start_idx = breakout_idx 
            if search_start_idx >= len(closes) - 1: break
            
    else:
        while level_count <= 3:
            # 1. Find the first major High after the current search point (The initial rise)
            valid_highs = raw_highs[raw_highs > search_start_idx]
            if len(valid_highs) == 0: break
            sh_idx = valid_highs[0]
            box_top = float(highs[sh_idx])
            
            # 2. Track the pullback Low until price breaks above the box top
            breakout_idx = len(closes) - 1
            box_bottom = float(lows[sh_idx]) # Initialize at the high

            for j in range(sh_idx + 1, len(closes)):
                # If price pulls back lower, stretch the bottom of the box
                if lows[j] < box_bottom:
                    box_bottom = float(lows[j])
                # If price rallies above the top, the consolidation is over
                if closes[j] > box_top:
                    breakout_idx = j
                    break
            
            # Only draw if the box isn't pure micro-noise
            if (box_top - box_bottom) >= (atr * 0.3):
                boxes.append({
                    "time": int(dates[sh_idx]),
                    "end_time": int(dates[breakout_idx]),
                    "top": box_top,
                    "bottom": box_bottom,
                    "type": "BULL_CONS",
                    "label": f"LEVEL {level_count}",
                })
                level_count += 1
            
            # Move the search index forward to the breakout candle so boxes can't overlap
            search_start_idx = breakout_idx 
            if search_start_idx >= len(closes) - 1: break
            
    return boxes

def analyze_market_structure(df: pd.DataFrame, profile: Dict) -> Dict:
    """
    MARKET MAKER METHOD (MMM) CYCLE ANALYSIS
    1. Find Macro Peak (M/W Anchor)
    2. Reset cycle if 200 EMA & 50 EMA invalidates it.
    3. Divide trend into 3 Levels using ATR
    4. Spot pullbacks to EMAs for entry
    """
    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values

    atr = calculate_atr(df, 14)
    if atr == 0:
        atr = float(df['Close'].mean()) * 0.001

    swing_order = adaptive_swing_order(df, atr)
    raw_highs = argrelextrema(highs, np.greater, order=swing_order)[0]
    raw_lows  = argrelextrema(lows,  np.less,    order=swing_order)[0]

    if len(raw_highs) == 0: raw_highs = np.array([int(np.argmax(highs))])
    if len(raw_lows)  == 0: raw_lows  = np.array([int(np.argmin(lows))])

    ANCHOR_LOOKBACK = min(len(closes), 600)  
    search_highs = raw_highs[raw_highs >= len(closes) - ANCHOR_LOOKBACK]
    search_lows  = raw_lows[raw_lows   >= len(closes) - ANCHOR_LOOKBACK]

    if len(search_highs) == 0: search_highs = raw_highs[-3:]
    if len(search_lows)  == 0: search_lows  = raw_lows[-3:]

    # Consequence Scoring for Anchor Peak
    best_high_score, best_high_idx = -1.0, int(search_highs[-1])
    for sh in search_highs:
        subsequent_low = float(np.min(lows[sh:])) if sh < len(lows) - 1 else float(highs[sh])
        drop = float(highs[sh]) - subsequent_low
        if drop > best_high_score:
            best_high_score, best_high_idx = drop, int(sh)

    best_low_score, best_low_idx = -1.0, int(search_lows[-1])
    for sl in search_lows:
        subsequent_high = float(np.max(highs[sl:])) if sl < len(highs) - 1 else float(lows[sl])
        rally = subsequent_high - float(lows[sl])
        if rally > best_low_score:
            best_low_score, best_low_idx = rally, int(sl)

    use_bearish = best_high_score > best_low_score

    # ── MMM PEAK RESET / INVALIDATION (The "Water & Mayo" Rule) ──────────────
    ema_200_series = df['Close'].ewm(span=200, adjust=False).mean()
    ema_50_series  = df['Close'].ewm(span=50, adjust=False).mean()
    
    current_price   = float(closes[-1])
    current_ema_200 = float(ema_200_series.iloc[-1])
    current_ema_50  = float(ema_50_series.iloc[-1])

    if use_bearish:
        if current_price > current_ema_200 and current_ema_50 > current_ema_200:
            use_bearish = False
            slice_lows = lows[best_high_idx:]
            if len(slice_lows) > 0:
                best_low_idx = best_high_idx + int(np.argmin(slice_lows))
    else:
        if current_price < current_ema_200 and current_ema_50 < current_ema_200:
            use_bearish = True
            slice_highs = highs[best_low_idx:]
            if len(slice_highs) > 0:
                best_high_idx = best_low_idx + int(np.argmax(slice_highs))
    # ─────────────────────────────────────────────────────────────────────────

    if use_bearish:
        cycle        = "BEARISH CYCLE (Peak M)"
        anchor_idx   = best_high_idx
        anchor_price = float(highs[anchor_idx])
        pattern_name = "Peak Formation High (M)"
        anchor_color = "rgba(255, 59, 59, 1)"
        total_move   = anchor_price - float(closes[-1])
    else:
        cycle        = "BULLISH CYCLE (Peak W)"
        anchor_idx   = best_low_idx
        anchor_price = float(lows[anchor_idx])
        pattern_name = "Peak Formation Low (W)"
        anchor_color = "rgba(59, 255, 130, 1)"
        total_move   = float(closes[-1]) - anchor_price

    # Stop Hunts / Pins
    sweeps = detect_liquidity_sweeps(df, raw_highs, raw_lows, atr)
    
    # 🟢 MMM Consolidation Boxes
    consolidation_boxes = detect_mmm_consolidations(df, anchor_idx, cycle, raw_highs, raw_lows, atr)

    # MMM Levels (1.5x ATR per level of displacement)
    level_size = atr * 1.5 
    current_level = int(total_move // level_size) if level_size > 0 else 0
    
    if current_level > 3: current_level = 3
    if current_level < 0: current_level = 0

    # Determine Pullback vs Expansion Phase
    in_pullback = False
    if cycle.startswith("BEARISH"):
        last_swing_low = raw_lows[-1] if len(raw_lows) > 0 else 0
        last_swing_high = raw_highs[-1] if len(raw_highs) > 0 else 0
        in_pullback = last_swing_high > last_swing_low and last_swing_high > anchor_idx
    else:
        last_swing_low = raw_lows[-1] if len(raw_lows) > 0 else 0
        last_swing_high = raw_highs[-1] if len(raw_highs) > 0 else 0
        in_pullback = last_swing_low > last_swing_high and last_swing_low > anchor_idx

    # Package the Anchor line identical to how the frontend handled BOS/OBs
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
        "level":           current_level + 1,  # 1-indexed for display
        "in_pullback":     in_pullback,
        "lines":           all_lines,
        "anchor":          anchor_price,
        "anchor_idx":      int(anchor_idx),
        "anchor_high_idx": int(best_high_idx),
        "anchor_low_idx":  int(best_low_idx),
        "atr":             atr,
        "sweeps":          sweeps,
        "consolidation_boxes": consolidation_boxes
    }

# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DECISION ENGINE (MMM BASED)
# ─────────────────────────────────────────────────────────────────────────────

def score_mmm_setup(current_price: float, ema_50: float, ema_200: float, rsi: float, 
                    level: int, in_pullback: bool, cycle: str, sweep_nearby: bool, atr: float) -> int:
    """Scores MMM entries: Focuses on Pullbacks to the 50 EMA during Level 1 and 2."""
    score = 50 
    dist_to_ema = abs(current_price - ema_50)

    # Reward Levels 1 and 2. Penalize Level 3 (Exhaustion/Reversal zone)
    if level in [1, 2]:
        score += 15
    elif level >= 3:
        score -= 20

    # Entry trigger: Pinning/Touching the 50 EMA (The "Water" in MMM terms)
    if dist_to_ema <= atr * 0.5:
        score += 20
        
    # Trend alignment (50 EMA crossing 200 EMA)
    if cycle.startswith("BULLISH") and ema_50 > ema_200:
        score += 10
    elif cycle.startswith("BEARISH") and ema_50 < ema_200:
        score += 10

    # Stop Hunts (Pins to the high/low create strong confluence in MMM)
    if sweep_nearby:
        score += 10

    return min(max(score, 0), 95)

def calculate_trade_levels(current_price: float, signal: str,
                            atr: float, decimals: int, ema_50: float) -> Optional[Dict]:
    try:
        entry = current_price
        
        # Stop Loss goes behind the 50 EMA + buffer
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

        # RUN MMM ANALYSIS
        ms = analyze_market_structure(df, profile)
        cycle         = ms['cycle']
        current_level = ms['level']
        in_pullback   = ms['in_pullback']
        lines         = ms['lines']
        sweeps        = ms['sweeps']
        boxes         = ms.get('consolidation_boxes', [])
        
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
        
        phase_string = f"LEVEL {current_level} {'(PULLBACK)' if in_pullback else '(EXPANSION)'}"

        reasoning = [
            f"🧭 Market Maker Bias: {cycle}",
            f"📊 Phase: {phase_string}",
            f"📈 Price vs Macro Trend: {ema_bias}",
            news_string,
        ]

        if sweep_str: reasoning.append(sweep_str)

        # MMM Trading Logic: Enter on pullbacks to the 50 EMA
        dist = abs(current_price - ema_50)
        
        if current_level >= 3 and not in_pullback:
            reasoning.append("⏳ Level 3 Exhaustion. Anticipating macro reversal or reset.")
        elif not in_pullback:
            reasoning.append("🔄 Expansion phase active. Waiting for pullback to 50 EMA before entering.")
        else:
            if cycle.startswith("BULLISH"):
                if current_price >= ema_50 and dist <= (atr * 0.5):
                    signal = "BUY"
                    confidence = score_mmm_setup(current_price, ema_50, ema_200, rsi, current_level, in_pullback, cycle, sweep_nearby, atr)
                    reasoning.append(f"🔥 KILLZONE: Pullback to 50 EMA ({ema_50:.{decimals}f}) for Level {current_level} continuation.")
                else:
                    reasoning.append(f"📍 Pulling back. Waiting for tap on 50 EMA ({ema_50:.{decimals}f}).")
            else:
                if current_price <= ema_50 and dist <= (atr * 0.5):
                    signal = "SELL"
                    confidence = score_mmm_setup(current_price, ema_50, ema_200, rsi, current_level, in_pullback, cycle, sweep_nearby, atr)
                    reasoning.append(f"🔥 KILLZONE: Pullback to 50 EMA ({ema_50:.{decimals}f}) for Level {current_level} continuation.")
                else:
                    reasoning.append(f"📍 Pulling back. Waiting for tap on 50 EMA ({ema_50:.{decimals}f}).")

        # ML Override (feeding neutral SMC dummy data to not break the pickle file)
        if signal != "NEUTRAL" and ML_MODEL:
            try:
                features = pd.DataFrame([{
                    'type':           1 if signal == "BUY" else 0,
                    'fvg_size_pips':  0.0,  # Neutralized
                    'rsi_at_entry':   rsi,
                    'atr_at_entry':   atr,
                    'momentum_ratio': 1.0,  # Neutralized
                    'news_bias':      news_val
                }])
                prob = ML_MODEL.predict_proba(features)[0][1]
                ml_conf = int(prob * 100)
                confidence = int((confidence + ml_conf) / 2)  
                reasoning.append(f"🧠 ML Prediction: {ml_conf}% win probability.")
            except Exception as ml_e:
                logger.warning(f"ML inference failed: {ml_e}")

        if confidence == 0 and signal != "NEUTRAL":
            confidence = 60  

        trade_setup = None
        if signal in ("BUY", "SELL") and confidence >= 65:
            trade_setup = calculate_trade_levels(current_price, signal, atr, decimals, ema_50)
            if trade_setup:
                reasoning.append(
                    f"📐 Setup: Entry {trade_setup['entry']} | SL {trade_setup['stop_loss']} | TP {trade_setup['take_profit']} | RR 1:{trade_setup['risk_reward']}"
                )

        # 🟢 Frontend Payload Integrity: 
        # Passing our dynamically generated consolidation boxes into the visuals payload!
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
                "smc_zones":  boxes,  # 🟢 Maps perfectly to the new ChartComponent's box renderer
                "bos_lines":  lines,  
                "fvgs":       [],     
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
    🔬 DIAGNOSTIC ENDPOINT — Fully converted to MMM
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

        ms = analyze_market_structure(df, profile)

        for h in high_scores:
            if h["candle_idx"] == ms.get("anchor_high_idx", -1):
                h["is_chosen"] = True
        for l in low_scores:
            if l["candle_idx"] == ms.get("anchor_low_idx", -1):
                l["is_chosen"] = True

        return {
            "✅ ENGINE VERSION":    "AuraBrain MMM v1.1",
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
            "📦 CONSOLIDATIONS":   ms.get('consolidation_boxes', []),
            "🎯 SWEEPS (PINS)":    ms.get('sweeps', []),
            "─── VERDICT ───": "──────────────────────────────────────",
            "🧭 BIAS":             ms['cycle'],
            "📍 WAITING FOR":      f"Price to tap 50 EMA" if ms['in_pullback'] else "Expansion to complete before pullback",
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