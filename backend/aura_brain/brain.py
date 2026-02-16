import os
import pandas as pd
import numpy as np
import math
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sklearn.preprocessing import MinMaxScaler
from scipy.stats import pearsonr
from scipy.signal import argrelextrema
import logging
from contextlib import asynccontextmanager
import httpx
from starlette.responses import Response

# ✅ CORRECT IMPORTS
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator 

# ⚙️ SETTINGS
CSV_FILENAME = "1h.csv" 
PATTERN_SIZE = 60       
MAX_SCAN_LIMIT = 50000
NODE_URL = "http://127.0.0.1:10000"  # ⚡ Internal Node.js URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

# 🧠 GLOBAL MEMORY
MARKET_MEMORY = {"df": None}

# --- HELPER: SAFE NUMBER ---
def safe_float(value, default=0.0):
    try:
        if value is None: return default
        num = float(value)
        if math.isnan(num) or math.isinf(num):
            return default
        return num
    except:
        return default

# --- 1. DATA ENGINE (UNCHANGED) ---
def load_data_into_memory():
    """Loads CSV once and stores it in RAM"""
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(base_dir, CSV_FILENAME)

        logger.info(f"📂 Pre-loading database from: {file_path}")

        if not os.path.exists(file_path):
            if os.path.exists(CSV_FILENAME):
                file_path = CSV_FILENAME
            else:
                logger.error(f"❌ File not found: {CSV_FILENAME}")
                return None

        # READ CSV (Robust)
        try:
            df = pd.read_csv(file_path, sep=';')
            if len(df.columns) < 2:
                df = pd.read_csv(file_path, sep=',')
        except:
            df = pd.read_csv(file_path, sep=',')
        
        # CLEANUP
        df.columns = [c.lower().strip() for c in df.columns]
        rename_map = {
            'close': 'Close', 'c': 'Close', '<close>': 'Close',
            'high': 'High', 'h': 'High', '<high>': 'High',
            'low': 'Low', 'l': 'Low', '<low>': 'Low',
            'open': 'Open', 'o': 'Open', '<open>': 'Open',
            'date': 'Date', 'time': 'Date', 'timestamp': 'Date', '<date>': 'Date'
        }
        df.rename(columns=rename_map, inplace=True)
        
        numeric_cols = ['Open', 'High', 'Low', 'Close']
        for col in numeric_cols:
            if col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].astype(str).str.replace(',', '.')
                df[col] = pd.to_numeric(df[col], errors='coerce')

        df.dropna(subset=numeric_cols, inplace=True)

        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
            df.dropna(subset=['Date'], inplace=True)
            df.set_index('Date', inplace=True)
            df.sort_index(inplace=True)

        logger.info(f"✅ CACHED {len(df)} candles in RAM.")
        return df

    except Exception as e:
        logger.error(f"❌ Cache Init Failed: {e}")
        return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    MARKET_MEMORY["df"] = load_data_into_memory()
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
    currency: str = "USD"

# --- 2. SMART TRENDLINE ENGINE (TIGHTER & CLOSER) ---
def detect_key_levels(df):
    """
    Finds PIVOT points relative to CURRENT price.
    Only keeps lines within 5% of current price to avoid 'Lagging Lines'.
    """
    try:
        if df.empty: return []
        
        work_df = df.reset_index()
        close_prices = work_df['Close'].values
        current_price = close_prices[-1]
        last_index = len(close_prices) - 1
        
        # Order=5 means local peak for 5 candles (Finds tighter, closer pivots)
        order = 5 
        
        resistance_idxs = argrelextrema(close_prices, np.greater, order=order)[0]
        support_idxs = argrelextrema(close_prices, np.less, order=order)[0]
        
        lines = []

        # 1. RESISTANCE (Above Price & Within 5% range)
        valid_res = [i for i in resistance_idxs if close_prices[i] > current_price and close_prices[i] < current_price * 1.05]
        valid_res.sort(key=lambda i: close_prices[i]) # Closest first
        
        for idx in valid_res[:2]: # Top 2 closest
            lines.append({
                "type": "RESISTANCE",
                "price": float(close_prices[idx]),
                "start_index": int(idx),
                "end_index": int(last_index)
            })

        # 2. SUPPORT (Below Price & Within 5% range)
        valid_sup = [i for i in support_idxs if close_prices[i] < current_price and close_prices[i] > current_price * 0.95]
        valid_sup.sort(key=lambda i: close_prices[i], reverse=True) # Closest first
        
        for idx in valid_sup[:2]: # Top 2 closest
            lines.append({
                "type": "SUPPORT",
                "price": float(close_prices[idx]),
                "start_index": int(idx),
                "end_index": int(last_index)
            })
            
        return lines
    except Exception as e:
        logger.warning(f"⚠️ Trendline detection failed: {e}")
        return []

# --- 3. FRACTAL PATTERN RECOGNITION (NORMALIZED) ---
def find_fractals(df):
    """
    Finds historical matches and PROJECTS them onto current price.
    Fixes the 'Wild Jump' by normalizing the ghost line to start at current price.
    """
    try:
        recent_data = df.iloc[-(MAX_SCAN_LIMIT + PATTERN_SIZE):]
        prices = recent_data['Close'].values
        current_price_level = prices[-1] # The live price right now
        
        if len(prices) < PATTERN_SIZE + 24: return []
        current_pattern = prices[-PATTERN_SIZE:]
        
        if np.any(np.isnan(current_pattern)) or np.any(np.isinf(current_pattern)): return []
        
        scaler = MinMaxScaler()
        try:
            current_norm = scaler.fit_transform(current_pattern.reshape(-1, 1)).flatten()
        except: return []
        
        matches = []
        history_limit = len(prices) - PATTERN_SIZE - 24 
        
        # Scan every 3rd candle
        for i in range(0, history_limit, 3):
            try:
                candidate = prices[i : i + PATTERN_SIZE]
                if len(candidate) != PATTERN_SIZE: continue
                
                candidate_scaler = MinMaxScaler()
                candidate_norm = candidate_scaler.fit_transform(candidate.reshape(-1, 1)).flatten()
                
                corr, _ = pearsonr(current_norm, candidate_norm)
                
                if corr > 0.80: 
                    # SHORTEN HORIZON: Only project next 15 candles (Clean Forecast)
                    forecast_horizon = 15
                    if i + PATTERN_SIZE + forecast_horizon < len(prices):
                        
                        # RAW Historical Data
                        hist_start_price = prices[i + PATTERN_SIZE - 1] # Price at end of matched pattern
                        raw_ghost_slice = prices[i + PATTERN_SIZE : i + PATTERN_SIZE + forecast_horizon]
                        
                        # 🧠 NORMALIZE: Shift the ghost to attach to CURRENT price
                        # Formula: (Historical_Point - Historical_Start) + Current_Price
                        price_diff = current_price_level - hist_start_price
                        normalized_ghost = [p + price_diff for p in raw_ghost_slice]

                        future_price_change = (raw_ghost_slice[-1] - hist_start_price) / hist_start_price
                        outcome = "BULLISH" if future_price_change > 0 else "BEARISH"
                        
                        matches.append({
                            "outcome": outcome, 
                            "similarity": safe_float(corr),
                            "future_change": safe_float(future_price_change * 100),
                            "start_date": str(recent_data.index[i]),
                            "plot_data": normalized_ghost # <--- SENDING SHIFTED DATA
                        })
            except: continue
                
        matches.sort(key=lambda x: x['similarity'], reverse=True)
        return matches[:5]
        
    except Exception as e:
        logger.error(f"⚠️ Fractal Scan Error: {e}")
        return []

# --- 4. API ENDPOINT (DYNAMIC SCOREBOARD) ---
@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    df = MARKET_MEMORY["df"]
    
    if df is None or df.empty:
        return {
            "signal": "HOLD", "confidence": 0, "trend": "LOADING",
            "reasoning": ["System initializing..."], 
            "keyLevels": {"resistance": 0, "support": 0, "ema": 0}
        }

    try:
        current_price = safe_float(df['Close'].iloc[-1])
        
        # A. Technicals
        try:
            ema_series = EMAIndicator(close=df['Close'], window=200).ema_indicator()
            ema_200 = safe_float(ema_series.iloc[-1], current_price)
        except: ema_200 = current_price
        
        try:
            rsi_series = RSIIndicator(close=df['Close'], window=14).rsi()
            rsi = safe_float(rsi_series.iloc[-1], 50.0)
        except: rsi = 50.0

        trend = "UPTREND" if current_price > ema_200 else "DOWNTREND"
        
        # B. Get Visuals
        trendlines = detect_key_levels(df)
        matches = find_fractals(df)
        
        # C. Scoreboard
        bull_score = 50
        bear_score = 50
        
        if current_price > ema_200: bull_score += 10
        else: bear_score += 10
        
        if rsi < 30: bull_score += 15 
        elif rsi > 70: bear_score += 15 
        
        best_match = None
        if matches:
            best_match = matches[0]
            sim_bonus = 20 * best_match['similarity']
            if best_match['outcome'] == "BULLISH": bull_score += sim_bonus
            else: bear_score += sim_bonus

        final_signal = "HOLD"
        final_confidence = 0
        
        if bull_score > bear_score:
            final_signal = "BUY"
            final_confidence = bull_score
        elif bear_score > bull_score:
            final_signal = "SELL"
            final_confidence = bear_score
            
        final_confidence = min(98, int(final_confidence))

        return {
            "signal": final_signal,
            "confidence": final_confidence,
            "trend": trend,
            "pattern": f"Deep Scan ({len(matches)} Matches)",
            "reasoning": [
                f"Trend: {trend} (EMA 200)",
                f"RSI: {round(rsi, 2)}",
                f"History: {int(best_match['similarity']*100)}% match found" if best_match else "No match",
                f"Projection: {best_match['outcome']}" if best_match else "Flat"
            ],
            "keyLevels": {
                "resistance": trendlines[0]['price'] if trendlines else current_price * 1.02,
                "support": trendlines[-1]['price'] if trendlines else current_price * 0.98,
                "ema": ema_200
            },
            "visuals": {
                "lines": trendlines,
                "fractal": best_match
            }
        }

    except Exception as e:
        logger.error(f"❌ Analysis Crash: {e}", exc_info=True)
        return {
            "signal": "NEUTRAL", "confidence": 0, "trend": "NEUTRAL", 
            "reasoning": ["Error calculating signals"],
            "keyLevels": {"resistance": 0, "support": 0, "ema": 0}
        }

# ⚡ PROXY (UNCHANGED)
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_to_node(path: str, request: Request):
    try:
        url = f"{NODE_URL}/{path}"
        query_string = str(request.url.query)
        if query_string: url = f"{url}?{query_string}"
        
        body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            try: body = await request.body()
            except: body = None
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=request.method,
                url=url,
                headers={k: v for k, v in request.headers.items() if k.lower() not in ['host', 'content-length']},
                content=body
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers={k: v for k, v in dict(response.headers).items() if k.lower() not in ['content-encoding', 'transfer-encoding', 'connection']}
            )
    except:
        raise HTTPException(status_code=502, detail="Gateway Error")

@app.get("/health")
async def health():
    return {"status": "healthy", "candles_loaded": len(MARKET_MEMORY["df"]) if MARKET_MEMORY["df"] is not None else 0}