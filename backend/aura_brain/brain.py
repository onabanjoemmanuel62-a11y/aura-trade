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

# ✅ TECHNICAL ANALYSIS LIBRARY
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator 

# 🧠 MACHINE LEARNING LIBRARY
import joblib

# ⚙️ SETTINGS
CSV_FILENAME = "1h.csv" 
NODE_URL = "http://127.0.0.1:10000" 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}

# ==========================================
# 🧠 LOAD THE MACHINE LEARNING MODEL
# ==========================================
base_dir = os.path.dirname(os.path.abspath(__file__))
model_path = os.path.join(base_dir, "aura_model.pkl")
try:
    ML_MODEL = joblib.load(model_path)
    logger.info("🧠 ML Brain 'aura_model.pkl' successfully loaded!")
except Exception as e:
    ML_MODEL = None
    logger.warning(f"⚠️ ML Brain not found. Falling back to rule-based confidence.")

def safe_float(value, default=0.0):
    try:
        if value is None: return default
        num = float(value)
        if math.isnan(num) or math.isinf(num): return default
        return num
    except:
        return default

def load_csv_fallback():
    try:
        file_path = os.path.join(os.path.dirname(base_dir), CSV_FILENAME)
        if not os.path.exists(file_path): return None
        try:
            df = pd.read_csv(file_path, sep=';')
            if len(df.columns) < 2: df = pd.read_csv(file_path, sep=',')
        except:
            df = pd.read_csv(file_path, sep=',')
        
        df.columns = [c.lower().strip() for c in df.columns]
        rename_map = {'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open', 'date': 'Date', 'time': 'Date', 'timestamp': 'Date'}
        df.rename(columns=rename_map, inplace=True)
        
        numeric_cols = ['Open', 'High', 'Low', 'Close']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        df.dropna(subset=numeric_cols, inplace=True)
        return df if len(df) > 50 else None
    except Exception as e:
        return None

def process_live_candles(candles_data: List[Dict]):
    try:
        df = pd.DataFrame(candles_data)
        rename_map = {'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open', 'time': 'Date', 'timestamp': 'Date'}
        df.rename(columns=rename_map, inplace=True)
        cols = ['Open', 'High', 'Low', 'Close']
        for c in cols:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        return df
    except Exception as e:
        return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    MARKET_MEMORY["df"] = load_csv_fallback()
    yield
    MARKET_MEMORY["df"] = None

app = FastAPI(lifespan=lifespan)
origins = ["*"] 
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class AnalysisRequest(BaseModel):
    timeframe: str = "1h"
    currency: str = "XAUUSD"
    current_price: float = 0.0
    candles: Optional[List[Dict]] = None
    htf_candles: Optional[List[Dict]] = None  
    news_data: Optional[Dict] = None

# =================================================================
# 🧠 V9 TRUE VISUAL ENGINE: THE FINAL ANCHOR FIX
# =================================================================
def detect_mmm_cycle(df):
    try:
        highs = df['High'].values
        lows = df['Low'].values
        closes = df['Close'].values
        opens = df['Open'].values
        dates = df['Date'].values if 'Date' in df.columns else df.index.values

        atr = pd.Series(highs - lows).rolling(14).mean().bfill().values[-1]
        if atr == 0: atr = 0.001

        # 1. FIND THE VISIBLE MACRO EXTREMES (Expanded to 300 candles for true weekly peaks)
        lookback = 300 
        start_idx = max(0, len(closes) - lookback)
        
        highest_idx = start_idx + np.argmax(highs[start_idx:])
        lowest_idx = start_idx + np.argmin(lows[start_idx:])

        # 2. THE CORRECTED ORIGIN TRACE LOGIC
        # If the LOWEST point happened AFTER the HIGHEST point...
        if lowest_idx > highest_idx:
            # The Low is the most recent extreme. Market bottomed out and is now RISING.
            cycle = "BULLISH"
            anchor_idx = lowest_idx
            anchor_price = lows[anchor_idx]
            pattern_name = "PFL ↑ (Anchor)"
        # If the HIGHEST point happened AFTER the LOWEST point...
        else:
            # The High is the most recent extreme. Market topped out and is now DROPPING.
            cycle = "BEARISH"
            anchor_idx = highest_idx
            anchor_price = highs[anchor_idx]
            pattern_name = "PFH ↓ (Anchor)"

        zones = []
        lines = [{
            "level": float(anchor_price),
            "start_time": int(dates[anchor_idx]),
            "end_time": int(dates[-1]),
            "type": pattern_name,
            "color": "rgba(255, 215, 0, 1)" # Solid Gold Peak
        }]

        # 3. VISUAL BOS & OB MAPPING (Preserved from V8 because it worked perfectly)
        current_level = 0
        state = "PUSHING" 
        
        if cycle == "BEARISH":
            current_low_val = anchor_price
            current_low_idx = anchor_idx
            pullback_high_val = None
            pullback_high_idx = None

            for i in range(anchor_idx + 1, len(closes)):
                if state == "PUSHING":
                    if lows[i] < current_low_val:
                        current_low_val = lows[i] 
                        current_low_idx = i
                    elif highs[i] > current_low_val + atr: 
                        state = "PULLBACK"
                        pullback_high_val = highs[i]
                        pullback_high_idx = i
                
                elif state == "PULLBACK":
                    if highs[i] > pullback_high_val:
                        pullback_high_val = highs[i] 
                        pullback_high_idx = i
                    elif closes[i] < current_low_val: 
                        current_level += 1
                        
                        lines.append({
                            "level": float(current_low_val),
                            "start_time": int(dates[current_low_idx]),
                            "end_time": int(dates[i]),
                            "type": f"BOS {current_level}",
                            "color": "rgba(33, 150, 243, 0.8)" 
                        })

                        # Find Trap Candle for OB
                        ob_idx = pullback_high_idx
                        for k in range(pullback_high_idx, max(anchor_idx, pullback_high_idx - 5), -1):
                            if closes[k] > opens[k]: # Last bullish candle before drop
                                ob_idx = k
                                break

                        # Draw tightly wrapped OB box
                        zones.append({
                            "type": "OB_BEAR", 
                            "top": float(highs[ob_idx]),
                            "bottom": float(lows[ob_idx]),
                            "price": float(lows[ob_idx]),
                            "time": int(dates[ob_idx]),
                            "is_mitigated": False,
                            "fvg_size_pips": float(abs(highs[ob_idx] - lows[ob_idx])),
                            "momentum_ratio": 2.0 
                        })

                        current_low_val = lows[i]
                        current_low_idx = i
                        state = "PUSHING"

        elif cycle == "BULLISH":
            current_high_val = anchor_price
            current_high_idx = anchor_idx
            pullback_low_val = None
            pullback_low_idx = None

            for i in range(anchor_idx + 1, len(closes)):
                if state == "PUSHING":
                    if highs[i] > current_high_val:
                        current_high_val = highs[i] 
                        current_high_idx = i
                    elif lows[i] < current_high_val - atr: 
                        state = "PULLBACK"
                        pullback_low_val = lows[i]
                        pullback_low_idx = i
                
                elif state == "PULLBACK":
                    if lows[i] < pullback_low_val:
                        pullback_low_val = lows[i] 
                        pullback_low_idx = i
                    elif closes[i] > current_high_val: 
                        current_level += 1
                        
                        lines.append({
                            "level": float(current_high_val),
                            "start_time": int(dates[current_high_idx]),
                            "end_time": int(dates[i]),
                            "type": f"BOS {current_level}",
                            "color": "rgba(33, 150, 243, 0.8)" 
                        })

                        # Find Trap Candle for OB
                        ob_idx = pullback_low_idx
                        for k in range(pullback_low_idx, max(anchor_idx, pullback_low_idx - 5), -1):
                            if closes[k] < opens[k]: # Last bearish candle before rally
                                ob_idx = k
                                break

                        # Draw tightly wrapped OB box
                        zones.append({
                            "type": "OB_BULL", 
                            "top": float(highs[ob_idx]),
                            "bottom": float(lows[ob_idx]),
                            "price": float(highs[ob_idx]),
                            "time": int(dates[ob_idx]),
                            "is_mitigated": False,
                            "fvg_size_pips": float(abs(highs[ob_idx] - lows[ob_idx])),
                            "momentum_ratio": 2.0 
                        })

                        current_high_val = highs[i]
                        current_high_idx = i
                        state = "PUSHING"

        display_level = min(3, current_level + 1)
        in_pullback = (state == "PULLBACK")

        return {
            "cycle": cycle,
            "level": display_level,
            "in_pullback": in_pullback,
            "zones": zones[-2:], # Keep UI clean: only show the 2 most recent OBs
            "lines": lines,
            "anchor": float(anchor_price)
        }
    except Exception as e:
        logger.error(f"MMM Logic Error: {e}")
        return {"cycle": "NEUTRAL", "level": 0, "in_pullback": False, "zones": [], "lines": [], "anchor": 0}

def calculate_trade_levels(current_price, signal, support, resistance, atr_value, decimals):
    try:
        entry = current_price
        atr_buffer = atr_value * 1.5 if atr_value else entry * 0.002
        
        if signal == "BUY":
            stop_loss = support if (support > 0 and support < entry) else entry - atr_buffer
            take_profit = resistance if (resistance > entry) else entry + (abs(entry - stop_loss) * 2)
        elif signal == "SELL":
            stop_loss = resistance if (resistance > 0 and resistance > entry) else entry + atr_buffer
            take_profit = support if (support < entry and support > 0) else entry - (abs(entry - stop_loss) * 2)
        else:
            return None
        
        return {
            "entry": round(entry, decimals),
            "stop_loss": round(stop_loss, decimals),
            "take_profit": round(take_profit, decimals),
            "risk_reward": round(abs(take_profit - entry) / max(0.00001, abs(entry - stop_loss)), 2)
        }
    except:
        return None

@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    if req.candles and len(req.candles) > 50:
        df = process_live_candles(req.candles)
        data_source = "LIVE_NODE_DATA"
    else:
        df = MARKET_MEMORY["df"]
        data_source = "CSV_FALLBACK"

    if df is None or df.empty:
        return {"signal": "HOLD", "confidence": 0, "reasoning": ["Waiting for data..."]}

    try:
        csv_last_price = safe_float(df['Close'].iloc[-1])
        current_price = req.current_price if req.current_price > 0 else csv_last_price
        
        decimals = 5 
        if current_price > 500: decimals = 2 
        elif current_price > 10: decimals = 3 
        
        ema_200 = safe_float(EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1], current_price)
        rsi = safe_float(RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1], 50.0)
        atr = safe_float((df['High'] - df['Low']).tail(14).mean(), current_price * 0.01)
        
        # --- EXECUTE MMM LOGIC ---
        mmm_data = detect_mmm_cycle(df)
        master_bias = f"{mmm_data.get('cycle', 'NEUTRAL')} CYCLE"
        current_level = mmm_data.get('level', 0)
        in_pullback = mmm_data.get('in_pullback', False)
        
        smc_zones = mmm_data.get("zones", [])
        bos_lines = mmm_data.get("lines", [])
        
        nearest_supp = next((z for z in reversed(smc_zones) if z['type'] == 'OB_BULL'), None)
        nearest_res = next((z for z in reversed(smc_zones) if z['type'] == 'OB_BEAR'), None)
        
        sup_level = nearest_supp['top'] if nearest_supp else current_price - (atr * 2)
        res_level = nearest_res['bottom'] if nearest_res else current_price + (atr * 2)

        news_bias = "NEUTRAL"
        news_val = 0
        news_string = "No recent impactful news data."
        
        if req.news_data:
            actual = safe_float(req.news_data.get('actual', 0))
            forecast = safe_float(req.news_data.get('forecast', 0))
            event_name = req.news_data.get('event', 'News Event')
            if actual > forecast:
                news_val = -1
                news_string = f"📰 {event_name}: Actual ({actual}) beat Forecast ({forecast})."
            elif actual < forecast:
                news_val = 1
                news_string = f"📰 {event_name}: Actual ({actual}) missed Forecast ({forecast})."

        signal = "NEUTRAL"
        confidence = 0
        base_conf = 0
        target_zone = None
        
        phase_string = f"LEVEL {current_level} {'(PULLBACK)' if in_pullback else '(EXPANSION)'}"
        
        strategy_logic = [
            f"🧭 Master Bias: {master_bias}", 
            f"📊 Phase: {phase_string}",
            news_string
        ]

        # 🛑 TRUE MMM EXECUTION LOGIC
        if current_level >= 4 and not in_pullback:
            strategy_logic.append("⏳ Exhaustion Reached: Do not trade. Waiting for new Anchor.")
        elif not in_pullback:
            strategy_logic.append(f"⏳ Market is pushing. Waiting for pullback to 1H-OB to enter.")
        else:
            if mmm_data['cycle'] == 'BULLISH' and nearest_supp:
                distance_to_ob = current_price - nearest_supp['top']
                if current_price <= nearest_supp['top'] and current_price >= (nearest_supp['bottom'] - (atr*0.5)): 
                    signal = "BUY"
                    target_zone = nearest_supp
                    base_conf = 78
                    strategy_logic.append(f"🔥 KILLZONE: Price tapped 1H-OB.")
                elif distance_to_ob <= atr:
                    strategy_logic.append(f"Approaching 1H-OB. Waiting for tap.")
                else:
                    strategy_logic.append(f"Pulling back. Waiting for price to reach 1H-OB.")
                    
            elif mmm_data['cycle'] == 'BEARISH' and nearest_res:
                distance_to_ob = nearest_res['bottom'] - current_price
                if current_price >= nearest_res['bottom'] and current_price <= (nearest_res['top'] + (atr*0.5)): 
                    signal = "SELL"
                    target_zone = nearest_res
                    base_conf = 78
                    strategy_logic.append(f"🔥 KILLZONE: Price tapped 1H-OB.")
                elif distance_to_ob <= atr:
                    strategy_logic.append(f"Approaching 1H-OB. Waiting for tap.")
                else:
                    strategy_logic.append(f"Rallying. Waiting for price to reach 1H-OB.")

        if signal != "NEUTRAL" and target_zone and ML_MODEL:
            try:
                features = pd.DataFrame([{
                    'type': 1 if signal == "BUY" else 0,
                    'fvg_size_pips': target_zone.get('fvg_size_pips', 0.0),
                    'rsi_at_entry': rsi,
                    'atr_at_entry': atr,
                    'momentum_ratio': target_zone.get('momentum_ratio', 1.0),
                    'news_bias': news_val
                }])
                prob = ML_MODEL.predict_proba(features)[0][1] 
                confidence = int(max(base_conf, prob * 100)) 
                strategy_logic.append(f"🧠 ML Neural Prediction: {confidence}% Win Prob.")
            except Exception:
                pass

        if confidence == 0 and signal != "NEUTRAL":
            confidence = base_conf

        trade_setup = calculate_trade_levels(current_price, signal, sup_level, res_level, atr, decimals)

        return {
            "signal": signal,
            "confidence": int(confidence),
            "trend": master_bias, 
            "pattern": f"MMM Pullback", 
            "reasoning": strategy_logic,
            "keyLevels": {"resistance": round(res_level, decimals), "support": round(sup_level, decimals), "ema": round(ema_200, decimals)},
            "visuals": {
                "smc_zones": smc_zones, 
                "bos_lines": bos_lines 
            },
            "tradeSetup": trade_setup if confidence >= 70 else None
        }

    except Exception as e:
        logger.error(f"Analysis Crash: {e}", exc_info=True)
        return {"signal": "ERROR", "confidence": 0, "reasoning": [str(e)]}

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_to_node(path: str, request: Request):
    try:
        url = f"{NODE_URL}/{path}"
        if request.url.query: url += f"?{request.url.query}"
        body = await request.body() if request.method in ["POST", "PUT"] else None
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=request.method, url=url,
                headers={k: v for k, v in request.headers.items() if k.lower() not in ['host', 'content-length']},
                content=body
            )
            return Response(content=response.content, status_code=response.status_code, headers=dict(response.headers))
    except:
        raise HTTPException(status_code=503)