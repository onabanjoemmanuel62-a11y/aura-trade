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
# CORE ENGINE  — "AURA MMM v2.1" (PUSH & TIME CONSTRAINTS)
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
    sweeps.sort(key=lambda x: x['sweep_idx'])
    return sweeps[-5:]

# 🟢 THE BULLETPROOF "EMA FAN" STATE MACHINE
def detect_mmm_consolidations(df: pd.DataFrame, anchor_idx: int, cycle: str, atr: float, ema_50: np.ndarray, ema_200: np.ndarray) -> List[Dict]:
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    
    boxes = []
    macro_push = atr * 3.5      # Massive push required to register a Level
    min_box_length = 6          # Must chop sideways for 6+ hours
    
    if cycle.startswith("BULLISH"):
        # 🛡️ THE GOLDEN RULE: Find where the 50 EMA crosses above the 200 EMA to confirm trend
        cross_idx = -1
        for i in range(anchor_idx, len(closes)):
            if ema_50[i] > ema_200[i]:
                cross_idx = i
                break
                
        if cross_idx == -1: return boxes # Trend not confirmed yet

        current_idx = anchor_idx

        # Map the 2 Macro Consolidations
        for box_num in range(1, 3):
            if current_idx >= len(closes) - min_box_length: break

            base_price = lows[anchor_idx] if box_num == 1 else boxes[-1]['top']
            peak_val = base_price
            peak_idx = current_idx
            pullback_idx = -1

            for i in range(current_idx, len(closes)):
                if highs[i] > peak_val:
                    peak_val = highs[i]
                    peak_idx = i

                # Must push a massive distance from base
                if peak_val - base_price >= macro_push:
                    # Wait for price to pull back down
                    if peak_val - lows[i] >= atr:
                        # Pullback must tap the 50 EMA
                        if lows[i] <= ema_50[i] + (atr * 0.8):
                            # 🛡️ CRITICAL: The pullback MUST happen AFTER the EMA Cross!
                            if box_num == 1 and i < cross_idx:
                                continue 
                            pullback_idx = i
                            break

            if pullback_idx == -1: break 

            # Lock the Box Dimensions
            box_top = peak_val
            box_bottom = lows[pullback_idx]
            breakout_idx = len(closes) - 1
            breakout_dir = "none"

            for i in range(pullback_idx, len(closes)):
                if lows[i] < box_bottom: box_bottom = lows[i]

                # Breakout Confirmation
                if closes[i] > box_top:
                    breakout_idx = i
                    breakout_dir = "up"
                    break
                elif closes[i] < box_bottom - atr: 
                    breakout_idx = i
                    breakout_dir = "down"
                    break

            if breakout_idx - peak_idx >= min_box_length:
                boxes.append({
                    "time": int(dates[peak_idx]),
                    "end_time": int(dates[breakout_idx]),
                    "top": float(box_top),
                    "bottom": float(box_bottom),
                    "type": "BULL_CONS",
                    "label": f"CONS {box_num} (Prep L{box_num + 1})",
                    "breakout_dir": breakout_dir
                })
                if breakout_dir != "up": break 
                current_idx = breakout_idx
            else:
                if breakout_dir == "up":
                    current_idx = breakout_idx
                    continue
                break

    elif cycle.startswith("BEARISH"):
        # 🛡️ THE GOLDEN RULE: Find where the 50 EMA crosses below the 200 EMA
        cross_idx = -1
        for i in range(anchor_idx, len(closes)):
            if ema_50[i] < ema_200[i]:
                cross_idx = i
                break
                
        if cross_idx == -1: return boxes 

        current_idx = anchor_idx

        for box_num in range(1, 3):
            if current_idx >= len(closes) - min_box_length: break

            base_price = highs[anchor_idx] if box_num == 1 else boxes[-1]['bottom']
            trough_val = base_price
            trough_idx = current_idx
            pullback_idx = -1

            for i in range(current_idx, len(closes)):
                if lows[i] < trough_val:
                    trough_val = lows[i]
                    trough_idx = i

                if base_price - trough_val >= macro_push:
                    if highs[i] - trough_val >= atr:
                        if highs[i] >= ema_50[i] - (atr * 0.8):
                            # 🛡️ CRITICAL: Pullback MUST happen AFTER the EMA Cross!
                            if box_num == 1 and i < cross_idx:
                                continue
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
                boxes.append({
                    "time": int(dates[trough_idx]),
                    "end_time": int(dates[breakout_idx]),
                    "top": float(box_top),
                    "bottom": float(box_bottom),
                    "type": "BEAR_CONS",
                    "label": f"CONS {box_num} (Prep L{box_num + 1})",
                    "breakout_dir": breakout_dir
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
    raw_highs = argrelextrema(highs, np.greater, order=swing_order)[0]
    raw_lows  = argrelextrema(lows,  np.less,    order=swing_order)[0]

    # ── TRUE MMM PEAK DETECTION (Tied to EMA Crossovers) ───────────
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
    # ───────────────────────────────────────────────────────────────

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
    
    # 🟢 Get the MACRO boxes
    consolidation_boxes = detect_mmm_consolidations(df, anchor_idx, cycle, atr, ema_50_array, ema_200_array)

    # 🟢 MMM TRADING LOGIC (No Level 1 Trades allowed)
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

# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DECISION ENGINE (MMM BASED)
# ─────────────────────────────────────────────────────────────────────────────

def score_mmm_setup(current_price: float, ema_50: float, ema_200: float, rsi: float, 
                    level: int, in_pullback: bool, cycle: str, sweep_nearby: bool, atr: float, phase_str: str) -> int:
    if "FAILED" in phase_str: return 0
        
    score = 50 
    dist_to_ema = abs(current_price - ema_50)

    if level in [1, 2]: score += 15
    elif level >= 3: score -= 20

    if dist_to_ema <= atr * 0.5: score += 20
        
    if cycle.startswith("BULLISH") and ema_50 > ema_200: score += 10
    elif cycle.startswith("BEARISH") and ema_50 < ema_200: score += 10

    if sweep_nearby: score += 10

    return min(max(score, 0), 95)

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

        ms = analyze_market_structure(df, profile)
        cycle         = ms['cycle']
        current_level = ms['level']
        phase_str     = ms['phase_str']
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
            # We are in a valid pullback (Cons 1 or Cons 2)
            if cycle.startswith("BULLISH"):
                if current_price >= ema_50 and dist <= (atr * 1.0):
                    signal = "BUY"
                    confidence = score_mmm_setup(current_price, ema_50, ema_200, rsi, current_level, in_pullback, cycle, sweep_nearby, atr, phase_str)
                    reasoning.append(f"🔥 KILLZONE: Entering on 50 EMA for {phase_str.split('(')[-1].strip(')')}.")
                else:
                    reasoning.append(f"📍 Pulling back. Waiting for tap on 50 EMA ({ema_50:.{decimals}f}).")
            else:
                if current_price <= ema_50 and dist <= (atr * 1.0):
                    signal = "SELL"
                    confidence = score_mmm_setup(current_price, ema_50, ema_200, rsi, current_level, in_pullback, cycle, sweep_nearby, atr, phase_str)
                    reasoning.append(f"🔥 KILLZONE: Entering on 50 EMA for {phase_str.split('(')[-1].strip(')')}.")
                else:
                    reasoning.append(f"📍 Pulling back. Waiting for tap on 50 EMA ({ema_50:.{decimals}f}).")

        if signal != "NEUTRAL" and ML_MODEL:
            try:
                features = pd.DataFrame([{
                    'type':           1 if signal == "BUY" else 0,
                    'fvg_size_pips':  0.0,
                    'rsi_at_entry':   rsi,
                    'atr_at_entry':   atr,
                    'momentum_ratio': 1.0, 
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
                "smc_zones":  boxes,
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

        ms = analyze_market_structure(df, profile)

        return {
            "✅ ENGINE VERSION":    "AuraBrain MMM v2.1",
            "📊 INSTRUMENT":       req.currency,
            "💰 CURRENT PRICE":    round(current_price, decimals),
            "─── STRUCTURE ───": "──────────────────────────────────────",
            "🎯 CYCLE":            ms['cycle'],
            "📊 PHASE":            ms['phase_str'],
            "📦 CONSOLIDATIONS":   ms.get('consolidation_boxes', []),
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