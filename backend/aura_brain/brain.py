import os
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sklearn.preprocessing import MinMaxScaler
from scipy.stats import pearsonr
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
MAX_SCAN_LIMIT = 20000
NODE_URL = "http://127.0.0.1:10000"  # ⚡ NEW: Node.js internal URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

# 🧠 GLOBAL MEMORY
MARKET_MEMORY = {"df": None}

# --- 1. DATA ENGINE ---
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

        try:
            df = pd.read_csv(file_path, sep=';')
            if len(df.columns) < 2:
                df = pd.read_csv(file_path, sep=',')
        except:
            df = pd.read_csv(file_path, sep=',')
        
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
            df['Date'] = pd.to_datetime(df['Date'])
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

def find_fractals(df):
    recent_data = df.tail(MAX_SCAN_LIMIT + PATTERN_SIZE) 
    prices = recent_data['Close'].values
    
    if len(prices) < PATTERN_SIZE + 24:
        return []

    current_pattern = prices[-PATTERN_SIZE:]
    scaler = MinMaxScaler()
    current_norm = scaler.fit_transform(current_pattern.reshape(-1, 1)).flatten()
    
    matches = []
    history_limit = len(prices) - PATTERN_SIZE - 24 
    
    for i in range(0, history_limit, 2):
        candidate = prices[i : i + PATTERN_SIZE]
        candidate_norm = scaler.fit_transform(candidate.reshape(-1, 1)).flatten()
        
        corr, _ = pearsonr(current_norm, candidate_norm)
        
        if corr > 0.85:
            future_price = prices[i + PATTERN_SIZE + 24]
            entry_price = prices[i + PATTERN_SIZE]
            outcome = "BULLISH" if future_price > entry_price else "BEARISH"
            matches.append({"outcome": outcome, "similarity": corr})
            
    return matches

# --- 3. API ENDPOINT ---
@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    df = MARKET_MEMORY["df"]
    
    if df is None or df.empty:
        raise HTTPException(status_code=503, detail="System initializing...")

    try:
        current_price = df['Close'].iloc[-1]
        
        ema_200 = EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1]
        rsi = RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1]
        
        trend = "UPTREND" if current_price > ema_200 else "DOWNTREND"
        
        matches = find_fractals(df)
        total_matches = len(matches)
        
        signal = "HOLD"
        confidence = 0

        if total_matches >= 3:
            bull_wins = len([m for m in matches if m['outcome'] == "BULLISH"])
            bear_wins = len([m for m in matches if m['outcome'] == "BEARISH"])
            
            if bull_wins > bear_wins:
                signal = "BUY"
                confidence = (bull_wins / total_matches) * 100
            else:
                signal = "SELL"
                confidence = (bear_wins / total_matches) * 100
            
            if (signal == "BUY" and trend == "DOWNTREND") or (signal == "SELL" and trend == "UPTREND"):
                confidence -= 15
            if (signal == "BUY" and rsi > 70) or (signal == "SELL" and rsi < 30):
                confidence -= 10

        return {
            "signal": signal,
            "confidence": int(max(0, min(99, confidence))),
            "trend": trend,
            "pattern": f"Deep Scan ({total_matches} Matches)",
            "reasoning": [
                f"Scanned {MAX_SCAN_LIMIT} recent candles (Speed Optimized).",
                f"Identified {total_matches} identical historical fractals.",
                f"{int(confidence)}% statistical probability.",
                f"Trend Filter: {trend}."
            ],
            "keyLevels": [
                round(df['High'].tail(50).max(), 2),
                round(df['Low'].tail(50).min(), 2),
                round(ema_200, 2)
            ]
        }

    except Exception as e:
        logger.error(f"Analysis Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ⚡ NEW: PROXY ALL OTHER REQUESTS TO NODE.JS
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_to_node(path: str, request: Request):
    """
    Forward all requests (except /api/analyze) to Node.js server
    This handles: /, /api/candles, /api/news, /api/trades, Socket.io
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{NODE_URL}/{path}"
            
            # Build query string
            query_string = str(request.url.query)
            if query_string:
                url = f"{url}?{query_string}"
            
            # Forward the request
            response = await client.request(
                method=request.method,
                url=url,
                headers={k: v for k, v in request.headers.items() 
                        if k.lower() not in ['host', 'content-length']},
                content=await request.body() if request.method in ["POST", "PUT"] else None
            )
            
            # Return response from Node.js
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=dict(response.headers)
            )
            
    except httpx.ConnectError:
        logger.error(f"❌ Cannot connect to Node.js at {NODE_URL}")
        raise HTTPException(status_code=503, detail="Node.js service unavailable")
    except Exception as e:
        logger.error(f"❌ Proxy error: {e}")
        raise HTTPException(status_code=500, detail=str(e))