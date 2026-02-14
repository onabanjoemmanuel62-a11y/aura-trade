import os
import pandas as pd
import numpy as np
import math
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
MAX_SCAN_LIMIT = 50000
NODE_URL = "http://127.0.0.1:10000"  # ⚡ Internal Node.js URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

# 🧠 GLOBAL MEMORY
MARKET_MEMORY = {"df": None}

# --- HELPER: SAFE NUMBER (Prevents 500 Errors) ---
def safe_float(value, default=0.0):
    """Converts anything to a float. Returns default if it fails."""
    try:
        if value is None: return default
        num = float(value)
        if math.isnan(num) or math.isinf(num):
            return default
        return num
    except:
        return default

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
        
        # CONVERT NUMBERS (Fix "1,23" -> 1.23)
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

# --- 2. FRACTAL PATTERN RECOGNITION (BULLETPROOF) ---
def find_fractals(df):
    """
    🛡️ CRASH-PROOF FRACTAL SCANNER
    Returns empty list if anything goes wrong, never raises exceptions
    """
    try:
        # Use simple tail to avoid index issues
        recent_data = df.iloc[-(MAX_SCAN_LIMIT + PATTERN_SIZE):]
        prices = recent_data['Close'].values
        
        if len(prices) < PATTERN_SIZE + 24:
            logger.warning(f"⚠️ Not enough data for fractals: {len(prices)} candles")
            return []

        # 🛡️ SAFE ARRAY OPERATIONS
        current_pattern = prices[-PATTERN_SIZE:]
        
        # Check for NaN or Inf in current pattern
        if np.any(np.isnan(current_pattern)) or np.any(np.isinf(current_pattern)):
            logger.warning("⚠️ Invalid values in current pattern")
            return []
        
        scaler = MinMaxScaler()
        try:
            current_norm = scaler.fit_transform(current_pattern.reshape(-1, 1)).flatten()
        except Exception as e:
            logger.error(f"⚠️ Normalization failed: {e}")
            return []
        
        matches = []
        history_limit = len(prices) - PATTERN_SIZE - 24 
        
        # Scan every 2nd candle
        for i in range(0, history_limit, 2):
            try:
                candidate = prices[i : i + PATTERN_SIZE]
                
                # Skip if wrong length
                if len(candidate) != PATTERN_SIZE:
                    continue
                
                # Skip if contains NaN/Inf
                if np.any(np.isnan(candidate)) or np.any(np.isinf(candidate)):
                    continue
                
                # 🛡️ SAFE NORMALIZATION
                candidate_scaler = MinMaxScaler()
                candidate_norm = candidate_scaler.fit_transform(candidate.reshape(-1, 1)).flatten()
                
                # 🛡️ SAFE CORRELATION
                if len(current_norm) != len(candidate_norm):
                    continue
                
                # Check for constant arrays (would cause correlation error)
                if np.std(current_norm) == 0 or np.std(candidate_norm) == 0:
                    continue
                
                corr, p_value = pearsonr(current_norm, candidate_norm)
                
                # Skip if correlation is NaN
                if math.isnan(corr) or math.isinf(corr):
                    continue
                
                if corr > 0.85: 
                    # Ensure we have enough data for future price
                    if i + PATTERN_SIZE + 24 < len(prices):
                        future_price = safe_float(prices[i + PATTERN_SIZE + 24])
                        entry_price = safe_float(prices[i + PATTERN_SIZE])
                        
                        if future_price > 0 and entry_price > 0:
                            outcome = "BULLISH" if future_price > entry_price else "BEARISH"
                            matches.append({
                                "outcome": outcome, 
                                "similarity": safe_float(corr),
                                "future_change": safe_float((future_price - entry_price) / entry_price * 100)
                            })
            except Exception as inner_e:
                # Skip this candidate and continue
                continue
                
        logger.info(f"✅ Fractal scan complete: {len(matches)} matches found")
        return matches
        
    except Exception as e:
        logger.error(f"⚠️ Fractal Scan Error: {e}")
        return []

# --- 3. API ENDPOINT (INDESTRUCTIBLE) ---
@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    df = MARKET_MEMORY["df"]
    
    if df is None or df.empty:
        # Return Loading State instead of 500
        return {
            "signal": "HOLD", "confidence": 0, "trend": "LOADING",
            "reasoning": ["System initializing data..."], 
            "keyLevels": {"resistance": 0, "support": 0, "ema": 0}
        }

    try:
        # 🛡️ SAFE CALCULATIONS
        current_price = safe_float(df['Close'].iloc[-1])
        
        # Initialize fallback values
        ema_200 = current_price
        rsi = 50.0
        
        # Calculate Technicals (Wrap in Try/Except)
        try:
            ema_series = EMAIndicator(close=df['Close'], window=200).ema_indicator()
            ema_200 = safe_float(ema_series.iloc[-1], current_price)
        except Exception as e:
            logger.warning(f"⚠️ EMA calculation failed: {e}")
            ema_200 = current_price
        
        try:
            rsi_series = RSIIndicator(close=df['Close'], window=14).rsi()
            rsi = safe_float(rsi_series.iloc[-1], 50.0)
        except Exception as e:
            logger.warning(f"⚠️ RSI calculation failed: {e}")
            rsi = 50.0

        trend = "UPTREND" if current_price > ema_200 else "DOWNTREND"
        
        # 🛡️ SAFE KEY LEVELS (Calculate BEFORE fractals)
        try:
            recent_high = safe_float(df['High'].tail(50).max(), current_price * 1.02)
            recent_low = safe_float(df['Low'].tail(50).min(), current_price * 0.98)
        except Exception as e:
            logger.warning(f"⚠️ Key levels calculation failed: {e}")
            recent_high = current_price * 1.02
            recent_low = current_price * 0.98
        
        # Fractals (Safe - returns empty list on error)
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
            
            # Adjust confidence based on trend
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
                f"Memory Scan Active.",
                f"Found {total_matches} historical patterns.",
                f"Trend: {trend} (EMA: {round(ema_200, 2)}).",
                f"RSI: {round(rsi, 2)}."
            ],
            # 📊 KEY LEVELS FOR CHART (Will never be 0 now)
            "keyLevels": {
                "resistance": recent_high,
                "support": recent_low,
                "ema": ema_200
            }
        }

    except Exception as e:
        logger.error(f"❌ Analysis Crash: {e}", exc_info=True)
        # 🛡️ FAILSAFE RESPONSE (Never return 500)
        # Return reasonable fallback values
        try:
            fallback_price = safe_float(df['Close'].iloc[-1], 5000.0)
        except:
            fallback_price = 5000.0
            
        return {
            "signal": "NEUTRAL", 
            "confidence": 0, 
            "trend": "NEUTRAL",
            "reasoning": [f"Analysis temporarily unavailable. System recovering..."], 
            "keyLevels": {
                "resistance": fallback_price * 1.02, 
                "support": fallback_price * 0.98, 
                "ema": fallback_price
            }
        }

# ⚡ PROXY ALL OTHER REQUESTS TO NODE.JS
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_to_node(path: str, request: Request):
    """
    Forward all requests (except /api/analyze) to Node.js server
    This handles: /, /api/candles, /api/news, /api/trades, Socket.io
    """
    try:
        # Build full URL
        url = f"{NODE_URL}/{path}"
        
        # Build query string
        query_string = str(request.url.query)
        if query_string:
            url = f"{url}?{query_string}"
        
        logger.info(f"🔄 Proxying {request.method} {path} -> {url}")
        
        # Get request body
        body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                body = await request.body()
            except:
                body = None
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Forward the request
            response = await client.request(
                method=request.method,
                url=url,
                headers={k: v for k, v in request.headers.items() 
                        if k.lower() not in ['host', 'content-length']},
                content=body
            )
            
            # Return response from Node.js
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers={k: v for k, v in dict(response.headers).items()
                        if k.lower() not in ['content-encoding', 'transfer-encoding', 'connection']}
            )
            
    except httpx.ConnectError:
        logger.error(f"❌ Cannot connect to Node.js at {NODE_URL}")
        raise HTTPException(status_code=503, detail="Node.js service unavailable")
    except Exception as e:
        logger.error(f"❌ Proxy error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# 💓 HEALTH CHECK
@app.get("/health")
async def health():
    """Health check for both Python and Node.js"""
    node_status = "unknown"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{NODE_URL}/healthcheck")
            node_status = "healthy" if response.status_code == 200 else "unhealthy"
    except:
        node_status = "unreachable"
    
    return {
        "python_brain": "healthy",
        "node_server": node_status,
        "data_loaded": MARKET_MEMORY["df"] is not None,
        "candles_in_memory": len(MARKET_MEMORY["df"]) if MARKET_MEMORY["df"] is not None else 0
    }