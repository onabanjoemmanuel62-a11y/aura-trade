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
# 🧠 V6 DIAGNOSTIC: ISOLATING THE TRUE M/W PEAK ANCHOR
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

        # 1. FIND THE MACRO ZONE (The true highest/lowest point of the week)
        lookback = 120 
        if len(highs) < lookback: lookback = len(highs)
        
        macro_high_idx = np.argmax(highs[-lookback:]) + (len(highs) - lookback)
        macro_low_idx = np.argmin(lows[-lookback:]) + (len(lows) - lookback)

        # 2. HUNT FOR THE TRAP CANDLE (Leg 2 of the M or W)
        if macro_high_idx > macro_low_idx:
            cycle = "BEARISH"
            anchor_idx = macro_high_idx
            
            # Scan the candles immediately at/after the macro high to find the actual Rejection Trap
            for i in range(macro_high_idx, min(len(closes), macro_high_idx + 10)):
                if closes[i] < opens[i]: # The first strong bearish rejection (Trap confirmed)
                    anchor_idx = i
                    break
            anchor_price = highs[anchor_idx]
            pattern_name = "'M' Peak Formation High"
        else:
            cycle = "BULLISH"
            anchor_idx = macro_low_idx
            
            # Scan the candles immediately at/after the macro low to find the actual Rejection Trap
            for i in range(macro_low_idx, min(len(closes), macro_low_idx + 10)):
                if closes[i] > opens[i]: # The first strong bullish rejection (Trap confirmed)
                    anchor_idx = i
                    break
            anchor_price = lows[anchor_idx]
            pattern_name = "'W' Peak Formation Low"

        # 3. DRAW ONLY THE SOLID GOLD ANCHOR LINE
        lines = [{
            "level": float(anchor_price),
            "start_time": int(dates[anchor_idx]),
            "end_time": int(dates[-1]),
            "type": f"{pattern_name} (Anchor)",
            "color": "rgba(255, 215, 0, 1)" # Solid Gold line
        }]

        return {
            "cycle": cycle,
            "level": 0,
            "in_pullback": False,
            "zones": [], # Disabled for isolation testing
            "lines": lines,
            "anchor": float(anchor_price),
            "pattern_name": pattern_name
        }
    except Exception as e:
        logger.error(f"MMM Logic Error: {e}")
        return {"cycle": "NEUTRAL", "level": 0, "in_pullback": False, "zones": [], "lines": [], "anchor": 0, "pattern_name": ""}

def calculate_trade_levels(current_price, signal, support, resistance, atr_value, decimals):
    return None # Disabled for isolation testing

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
        # --- EXECUTE DIAGNOSTIC MMM LOGIC ---
        mmm_data = detect_mmm_cycle(df)
        master_bias = f"{mmm_data.get('cycle', 'NEUTRAL')} CYCLE"
        pattern_name = mmm_data.get('pattern_name', '')
        
        strategy_logic = [
            f"🎯 ANCHOR ISOLATION MODE",
            f"🧭 Detected Trend: {master_bias}", 
            f"🔍 Pattern: {pattern_name}",
            "⚠️ Levels and Order Blocks are temporarily disabled for visual testing."
        ]

        return {
            "signal": "NEUTRAL",
            "confidence": 0,
            "trend": master_bias, 
            "pattern": f"Anchor Isolation Test", 
            "reasoning": strategy_logic,
            "keyLevels": {"resistance": 0, "support": 0, "ema": 0},
            "visuals": {
                "smc_zones": [], 
                "bos_lines": mmm_data.get("lines", []) 
            },
            "tradeSetup": None
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