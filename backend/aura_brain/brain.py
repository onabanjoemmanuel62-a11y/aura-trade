import os
import pandas as pd
import numpy as np
import math
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sklearn.preprocessing import MinMaxScaler
from scipy.stats import pearsonr
from scipy.signal import argrelextrema  # <--- NEW IMPORT FOR TRENDLINES
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

# --- 2. SMART TRENDLINE ENGINE (NEW) ---
def detect_key_levels(df):
    """
    Finds significant Support and Resistance lines using local Min/Max.
    Returns: List of lines with {price, type, start_index, end_index}
    """
    try:
        # We need numerical indices for the frontend to draw lines easily
        # So we reset index temporarily to get 0..N indices
        work_df = df.reset_index()
        close_prices = work_df['Close'].values
        
        # 'order' determines how significant the peak/valley must be. 
        # Higher = fewer, stronger lines. Lower = more noise.
        order = 15 
        
        # Find local peaks (Resistance) and valleys (Support)
        resistance_idxs = argrelextrema(close_prices, np.greater, order=order)[0]
        support_idxs = argrelextrema(close_prices, np.less, order=order)[0]
        
        lines = []
        last_index = len(close_prices) - 1

        # Process Resistance (Peaks) - Take last 2 strong ones
        for idx in resistance_idxs[-3:]: 
            price = float(close_prices[idx])
            lines.append({
                "type": "RESISTANCE",
                "price": price,
                "start_index": int(idx),
                "end_index": int(last_index) # Extend to current time
            })

        # Process Support (Valleys) - Take last 2 strong ones
        for idx in support_idxs[-3:]:
            price = float(close_prices[idx])
            lines.append({
                "type": "SUPPORT",
                "price": price,
                "start_index": int(idx),
                "end_index": int(last_index)
            })
            
        return lines
    except Exception as e:
        logger.warning(f"⚠️ Trendline detection failed: {e}")
        return []

# --- 3. FRACTAL PATTERN RECOGNITION (UPDATED FOR VISUALS) ---
def find_fractals(df):
    """
    🛡️ CRASH-PROOF FRACTAL SCANNER
    Now includes 'plot_data' for the frontend to draw the ghost pattern.
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
                
                if len(candidate) != PATTERN_SIZE: continue
                if np.any(np.isnan(candidate)) or np.any(np.isinf(candidate)): continue
                
                # 🛡️ SAFE NORMALIZATION
                candidate_scaler = MinMaxScaler()
                candidate_norm = candidate_scaler.fit_transform(candidate.reshape(-1, 1)).flatten()
                
                if len(current_norm) != len(candidate_norm): continue
                if np.std(current_norm) == 0 or np.std(candidate_norm) == 0: continue
                
                corr, p_value = pearsonr(current_norm, candidate_norm)
                
                if math.isnan(corr) or math.isinf(corr): continue
                
                if corr > 0.85: 
                    # Ensure we have enough data for future price
                    if i + PATTERN_SIZE + 24 < len(prices):
                        future_price = safe_float(prices[i + PATTERN_SIZE + 24])
                        entry_price = safe_float(prices[i + PATTERN_SIZE])
                        
                        # Grab the FUTURE data for visualization (The "Ghost")
                        # We take the pattern + 24 candles into the future
                        ghost_slice = prices[i : i + PATTERN_SIZE + 24]
                        
                        if future_price > 0 and entry_price > 0:
                            outcome = "BULLISH" if future_price > entry_price else "BEARISH"
                            matches.append({
                                "outcome": outcome, 
                                "similarity": safe_float(corr),
                                "future_change": safe_float((future_price - entry_price) / entry_price * 100),
                                "start_date": str(recent_data.index[i]), # For tooltip
                                "plot_data": ghost_slice.tolist() # <--- DATA FOR CHART
                            })
            except Exception as inner_e:
                continue
                
        logger.info(f"✅ Fractal scan complete: {len(matches)} matches found")
        return matches
        
    except Exception as e:
        logger.error(f"⚠️ Fractal Scan Error: {e}")
        return []

# --- 4. API ENDPOINT (UPDATED) ---
@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    df = MARKET_MEMORY["df"]
    
    if df is None or df.empty:
        return {
            "signal": "HOLD", "confidence": 0, "trend": "LOADING",
            "reasoning": ["System initializing data..."], 
            "keyLevels": {"resistance": 0, "support": 0, "ema": 0}
        }

    try:
        # 🛡️ SAFE CALCULATIONS
        current_price = safe_float(df['Close'].iloc[-1])
        
        ema_200 = current_price
        rsi = 50.0
        
        try:
            ema_series = EMAIndicator(close=df['Close'], window=200).ema_indicator()
            ema_200 = safe_float(ema_series.iloc[-1], current_price)
        except:
            ema_200 = current_price
        
        try:
            rsi_series = RSIIndicator(close=df['Close'], window=14).rsi()
            rsi = safe_float(rsi_series.iloc[-1], 50.0)
        except:
            rsi = 50.0

        trend = "UPTREND" if current_price > ema_200 else "DOWNTREND"
        
        # --- A. GENERATE VISUALS (LINES) ---
        trendlines = detect_key_levels(df)
        
        # --- B. RUN FRACTALS ---
        matches = find_fractals(df)
        total_matches = len(matches)
        
        signal = "HOLD"
        confidence = 0
        best_match = None

        if total_matches >= 1:
            # Sort by similarity to find the "Best Match" for the ghost overlay
            matches.sort(key=lambda x: x['similarity'], reverse=True)
            best_match = matches[0]

        if total_matches >= 3:
            bull_wins = len([m for m in matches if m['outcome'] == "BULLISH"])
            bear_wins = len([m for m in matches if m['outcome'] == "BEARISH"])
            
            if bull_wins > bear_wins:
                signal = "BUY"
                confidence = (bull_wins / total_matches) * 100
            else:
                signal = "SELL"
                confidence = (bear_wins / total_matches) * 100
            
            # Adjust confidence
            if (signal == "BUY" and trend == "DOWNTREND") or (signal == "SELL" and trend == "UPTREND"):
                confidence -= 15
            if (signal == "BUY" and rsi > 70) or (signal == "SELL" and rsi < 30):
                confidence -= 10

        # Construct Response with new "Visuals" block
        return {
            "signal": signal,
            "confidence": int(max(0, min(99, confidence))),
            "trend": trend,
            "pattern": f"Deep Scan ({total_matches} Matches)",
            "reasoning": [
                f"Memory Scan Active.",
                f"Found {total_matches} historical patterns similar to now.",
                f"Top Match: {int(best_match['similarity']*100)}% similarity ({best_match['start_date']})" if best_match else "No strong match found.",
                f"Trend: {trend} (EMA: {round(ema_200, 2)})."
            ],
            "keyLevels": {
                "resistance": trendlines[0]['price'] if trendlines else current_price * 1.02,
                "support": trendlines[1]['price'] if len(trendlines) > 1 else current_price * 0.98,
                "ema": ema_200
            },
            # 🎨 NEW VISUAL DATA FOR FRONTEND
            "visuals": {
                "lines": trendlines,       # Array of support/resistance lines
                "fractal": best_match      # The object containing 'plot_data' (Ghost Pattern)
            }
        }

    except Exception as e:
        logger.error(f"❌ Analysis Crash: {e}", exc_info=True)
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
    try:
        url = f"{NODE_URL}/{path}"
        query_string = str(request.url.query)
        if query_string:
            url = f"{url}?{query_string}"
        
        logger.info(f"🔄 Proxying {request.method} {path} -> {url}")
        
        body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                body = await request.body()
            except:
                body = None
        
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