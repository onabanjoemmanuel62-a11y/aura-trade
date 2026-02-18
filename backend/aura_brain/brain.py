import os
import pandas as pd
import numpy as np
import math
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
from sklearn.preprocessing import MinMaxScaler
from scipy.stats import pearsonr
from scipy.signal import argrelextrema
import logging
from contextlib import asynccontextmanager
import httpx
from starlette.responses import Response

# ✅ TECHNICAL ANALYSIS LIBRARY
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator 

# ⚙️ SETTINGS
CSV_FILENAME = "1h.csv" 
PATTERN_SIZE = 60       
MAX_SCAN_LIMIT = 5000   
NODE_URL = "http://127.0.0.1:10000" 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

# 🧠 GLOBAL MEMORY (Fallback only)
MARKET_MEMORY = {"df": None}

# --- HELPER: SAFE NUMBER ---
def safe_float(value, default=0.0):
    try:
        if value is None: return default
        num = float(value)
        if math.isnan(num) or math.isinf(num): return default
        return num
    except:
        return default

# --- 1. DATA ENGINE (MODIFIED FOR HYBRID LOADING) ---
def load_csv_fallback():
    """Loads CSV as a fallback if no live data is provided."""
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(base_dir, CSV_FILENAME)

        if not os.path.exists(file_path):
            return None

        # READ CSV
        try:
            df = pd.read_csv(file_path, sep=';')
            if len(df.columns) < 2: df = pd.read_csv(file_path, sep=',')
        except:
            df = pd.read_csv(file_path, sep=',')
        
        # CLEANUP & STANDARDIZE
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
        # Ensure we have enough data
        return df if len(df) > 50 else None

    except Exception as e:
        logger.error(f"❌ CSV Load Failed: {e}")
        return None

def process_live_candles(candles_data: List[Dict]):
    """Converts Node.js JSON candles into a Pandas DataFrame."""
    try:
        df = pd.DataFrame(candles_data)
        
        # Standardize Columns
        rename_map = {
            'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open', 
            'time': 'Date', 'timestamp': 'Date'
        }
        df.rename(columns=rename_map, inplace=True)
        
        # Ensure numeric types
        cols = ['Open', 'High', 'Low', 'Close']
        for c in cols:
            df[c] = pd.to_numeric(df[c], errors='coerce')
            
        return df
    except Exception as e:
        logger.error(f"❌ Live Data Conversion Failed: {e}")
        return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load CSV once on startup just in case
    MARKET_MEMORY["df"] = load_csv_fallback()
    logger.info(f"✅ BRAIN STARTED. CSV Memory: {len(MARKET_MEMORY['df']) if MARKET_MEMORY['df'] is not None else 0} candles.")
    yield
    MARKET_MEMORY["df"] = None

app = FastAPI(lifespan=lifespan)

# 🔒 SECURITY
origins = ["*"] # Open internally, locked by firewall in prod
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 📨 UPDATED REQUEST MODEL
class AnalysisRequest(BaseModel):
    timeframe: str = "1h"
    currency: str = "XAUUSD"
    current_price: float = 0.0
    candles: Optional[List[Dict]] = None  # 👈 NEW: Accepts live candle array

# --- 2. SMC ENGINE (UNCHANGED LOGIC) ---
def detect_smc_structures(df):
    zones = []
    try:
        opens = df['Open'].values
        closes = df['Close'].values
        highs = df['High'].values
        lows = df['Low'].values
        
        swing_period = 5
        swing_highs = argrelextrema(highs, np.greater, order=swing_period)[0]
        swing_lows = argrelextrema(lows, np.less, order=swing_period)[0]

        # A. BEARISH OB (Supply)
        for idx in swing_highs[-20:]: 
            ob_idx = idx
            subsequent_price = lows[ob_idx+1:]
            if len(subsequent_price) == 0: continue
            
            if np.min(subsequent_price) < lows[ob_idx]: # BOS
                ob_top = highs[ob_idx]
                ob_bottom = lows[ob_idx]
                
                # Mitigation Check
                is_tested = False
                for future_idx in range(ob_idx + 5, len(highs)):
                    if highs[future_idx] >= ob_bottom:
                        is_tested = True
                        break
                
                if not is_tested:
                    zones.append({"type": "OB_BEAR", "top": float(ob_top), "bottom": float(ob_bottom), "price": float(ob_bottom)})

        # B. BULLISH OB (Demand)
        for idx in swing_lows[-20:]:
            ob_idx = idx
            subsequent_price = highs[ob_idx+1:]
            if len(subsequent_price) == 0: continue
            
            if np.max(subsequent_price) > highs[ob_idx]: # BOS
                ob_top = highs[ob_idx]
                ob_bottom = lows[ob_idx]
                
                is_tested = False
                for future_idx in range(ob_idx + 5, len(lows)):
                    if lows[future_idx] <= ob_top:
                        is_tested = True
                        break
                
                if not is_tested:
                    zones.append({"type": "OB_BULL", "top": float(ob_top), "bottom": float(ob_bottom), "price": float(ob_top)})

        return zones
    except Exception as e:
        logger.warning(f"SMC Error: {e}")
        return []

# --- 3. FRACTAL PATTERN RECOGNITION ---
def find_fractals(df, live_price):
    try:
        recent_data = df.iloc[-(MAX_SCAN_LIMIT + PATTERN_SIZE):]
        prices = recent_data['Close'].values
        current_price = live_price if live_price > 0 else prices[-1]
        
        if len(prices) < PATTERN_SIZE + 24: return []
        current_pattern = prices[-PATTERN_SIZE:]
        
        scaler = MinMaxScaler()
        current_norm = scaler.fit_transform(current_pattern.reshape(-1, 1)).flatten()
        
        matches = []
        history_limit = len(prices) - PATTERN_SIZE - 24 
        
        for i in range(0, history_limit, 5):
            try:
                candidate = prices[i : i + PATTERN_SIZE]
                candidate_scaler = MinMaxScaler()
                candidate_norm = candidate_scaler.fit_transform(candidate.reshape(-1, 1)).flatten()
                
                corr, _ = pearsonr(current_norm, candidate_norm)
                
                if corr > 0.85: 
                    hist_entry_price = prices[i + PATTERN_SIZE - 1]
                    future_slice = prices[i + PATTERN_SIZE : i + PATTERN_SIZE + 24]
                    
                    normalized_ghost = []
                    for hist_price in future_slice:
                        percent_change = (hist_price - hist_entry_price) / hist_entry_price
                        projected_price = current_price * (1 + percent_change)
                        normalized_ghost.append(projected_price)

                    future_price = normalized_ghost[-1]
                    outcome = "BULLISH" if future_price > current_price else "BEARISH"
                    
                    matches.append({
                        "outcome": outcome, 
                        "similarity": safe_float(corr),
                        "future_change": safe_float((future_price - current_price) / current_price * 100),
                        "start_date": str(recent_data.index[i]), 
                        "plot_data": normalized_ghost 
                    })
            except: continue
                
        return matches
    except Exception as e:
        logger.error(f"Fractal Scan Error: {e}")
        return []

# --- 4. TRADE CALCULATOR ---
def calculate_trade_levels(current_price, signal, support, resistance, atr_value):
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
            "entry": round(entry, 2),
            "stop_loss": round(stop_loss, 2),
            "take_profit": round(take_profit, 2),
            "risk_reward": round(abs(take_profit - entry) / max(0.01, abs(entry - stop_loss)), 2)
        }
    except:
        return None

# --- 5. MAIN ENDPOINT (THE BRAIN) ---
@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    # 1. DECIDE DATA SOURCE: Live vs CSV
    if req.candles and len(req.candles) > 50:
        df = process_live_candles(req.candles)
        data_source = "LIVE_NODE_DATA"
    else:
        df = MARKET_MEMORY["df"]
        data_source = "CSV_FALLBACK"

    if df is None or df.empty:
        return {"signal": "HOLD", "confidence": 0, "reasoning": ["Waiting for data..."]}

    try:
        # 2. PREPARE METRICS
        csv_last_price = safe_float(df['Close'].iloc[-1])
        current_price = req.current_price if req.current_price > 0 else csv_last_price
        
        ema_200 = safe_float(EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1], current_price)
        rsi = safe_float(RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1], 50.0)
        atr = safe_float((df['High'] - df['Low']).tail(14).mean(), current_price * 0.01)
        trend = "UPTREND" if current_price > ema_200 else "DOWNTREND"
        
        # 3. RUN ALGORITHMS
        smc_zones = detect_smc_structures(df) 
        matches = find_fractals(df, current_price)
        
        # 4. FILTER ZONES
        bullish_zones = [z for z in smc_zones if z['type'] == 'OB_BULL' and z['top'] < current_price]
        bearish_zones = [z for z in smc_zones if z['type'] == 'OB_BEAR' and z['bottom'] > current_price]
        
        nearest_supp = max(bullish_zones, key=lambda z: z['top']) if bullish_zones else None
        nearest_res = min(bearish_zones, key=lambda z: z['bottom']) if bearish_zones else None
        
        sup_level = nearest_supp['top'] if nearest_supp else current_price * 0.985
        res_level = nearest_res['bottom'] if nearest_res else current_price * 1.015

        visual_zones = []
        if nearest_supp: visual_zones.append(nearest_supp)
        if nearest_res: visual_zones.append(nearest_res)

        # 5. GENERATE SIGNAL
        total_matches = len(matches)
        signal = "NEUTRAL"
        confidence = 0
        best_match = matches[0] if matches else None

        if total_matches >= 1:
            matches.sort(key=lambda x: x['similarity'], reverse=True)
            bull_votes = len([m for m in matches if m['outcome'] == "BULLISH"])
            bear_votes = len([m for m in matches if m['outcome'] == "BEARISH"])
            
            if bull_votes > bear_votes:
                signal = "BUY"
                confidence = (bull_votes / total_matches) * 100
            elif bear_votes > bull_votes:
                signal = "SELL"
                confidence = (bear_votes / total_matches) * 100
            
            # Confluence Factors
            if (signal == "BUY" and trend == "UPTREND") or (signal == "SELL" and trend == "DOWNTREND"):
                confidence += 15
            
            # RSI Filter
            if (signal == "BUY" and rsi > 70) or (signal == "SELL" and rsi < 30):
                confidence -= 25 # High risk
                
            # OB Confirmation (The "SMC" Stamp)
            if signal == "BUY" and nearest_supp and abs(current_price - nearest_supp['top']) < atr:
                confidence += 20 # Bouncing off Order Block
            if signal == "SELL" and nearest_res and abs(current_price - nearest_res['bottom']) < atr:
                confidence += 20 # Rejecting Order Block

        trade_setup = calculate_trade_levels(current_price, signal, sup_level, res_level, atr)

        return {
            "signal": signal,
            "confidence": int(max(0, min(99, confidence))),
            "trend": trend,
            "pattern": f"SMC + Fractal ({data_source})",
            "reasoning": [
                f"Market Structure: {trend}",
                f"Order Block Sup: {round(sup_level, 2)}",
                f"Order Block Res: {round(res_level, 2)}",
                f"RSI: {round(rsi, 1)}"
            ],
            "keyLevels": {"resistance": res_level, "support": sup_level, "ema": ema_200},
            "visuals": {
                "smc_zones": visual_zones, 
                "fractal": best_match
            },
            "tradeSetup": trade_setup
        }

    except Exception as e:
        logger.error(f"Analysis Crash: {e}", exc_info=True)
        return {"signal": "ERROR", "confidence": 0, "reasoning": [str(e)]}

# Proxy remains the same
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