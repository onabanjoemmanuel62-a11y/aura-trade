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

# ✅ TECHNICAL ANALYSIS LIBRARY
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
        if math.isnan(num) or math.isinf(num): return default
        return num
    except:
        return default

# --- 1. DATA ENGINE ---
def load_data_into_memory():
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(base_dir, CSV_FILENAME)

        if not os.path.exists(file_path):
            if os.path.exists(CSV_FILENAME): file_path = CSV_FILENAME
            else: return None

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
                if df[col].dtype == object: df[col] = df[col].astype(str).str.replace(',', '.')
                df[col] = pd.to_numeric(df[col], errors='coerce')

        df.dropna(subset=numeric_cols, inplace=True)
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
            df.dropna(subset=['Date'], inplace=True)
            df.set_index('Date', inplace=True)
            df.sort_index(inplace=True)

        logger.info(f"✅ SMC BRAIN LOADED: {len(df)} candles.")
        return df
    except Exception as e:
        logger.error(f"❌ Init Failed: {e}")
        return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    MARKET_MEMORY["df"] = load_data_into_memory()
    yield
    MARKET_MEMORY["df"] = None

app = FastAPI(lifespan=lifespan)

# 🔒 SECURITY: Whitelist Vercel & Render
origins = [
    "http://localhost:3000",
    "https://aura-trade-weld.vercel.app",  
    "https://aura-trade-v1.onrender.com"   
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalysisRequest(BaseModel):
    timeframe: str = "1h"
    currency: str = "USD"

# --- 2. SMC ENGINE (INSTITUTIONAL ZONES) ---
def detect_smc_structures(df):
    """
    Scans for Institutional Order Blocks and Fair Value Gaps (FVG).
    Returns a list of zones with {type, top, bottom, strength}.
    """
    zones = []
    try:
        prices = df['Close'].values
        highs = df['High'].values
        lows = df['Low'].values
        
        # 1. FAIR VALUE GAPS (FVG) - The "Magnet"
        # Look for 3-candle patterns with gaps
        for i in range(len(df)-300, len(df)): # Scan last 300 candles
            # Bullish FVG: Candle 1 High < Candle 3 Low
            if highs[i-2] < lows[i]:
                gap = lows[i] - highs[i-2]
                if gap > (prices[i] * 0.0005): # Filter tiny noise
                    zones.append({"type": "FVG_BULL", "price": lows[i], "top": lows[i], "bottom": highs[i-2]})

            # Bearish FVG: Candle 1 Low > Candle 3 High
            elif lows[i-2] > highs[i]:
                gap = lows[i-2] - highs[i]
                if gap > (prices[i] * 0.0005):
                    zones.append({"type": "FVG_BEAR", "price": highs[i], "top": lows[i-2], "bottom": highs[i]})

        # 2. ORDER BLOCKS (OB) - The "Wall"
        # Simplified: Swing Highs/Lows that led to a break
        swing_period = 5
        swing_highs = argrelextrema(highs, np.greater, order=swing_period)[0]
        swing_lows = argrelextrema(lows, np.less, order=swing_period)[0]

        # Get recent strong levels
        for idx in swing_highs[-10:]:
            zones.append({"type": "OB_BEAR", "price": highs[idx], "top": highs[idx], "bottom": lows[idx]})
            
        for idx in swing_lows[-10:]:
            zones.append({"type": "OB_BULL", "price": lows[idx], "top": highs[idx], "bottom": lows[idx]})

        return zones
    except Exception as e:
        logger.warning(f"⚠️ SMC Detection Failed: {e}")
        return []

# --- 3. FRACTAL PATTERN RECOGNITION (NORMALIZED) ---
def find_fractals(df):
    try:
        recent_data = df.iloc[-(MAX_SCAN_LIMIT + PATTERN_SIZE):]
        prices = recent_data['Close'].values
        current_price = prices[-1] 
        
        if len(prices) < PATTERN_SIZE + 24: return []

        current_pattern = prices[-PATTERN_SIZE:]
        scaler = MinMaxScaler()
        current_norm = scaler.fit_transform(current_pattern.reshape(-1, 1)).flatten()
        
        matches = []
        history_limit = len(prices) - PATTERN_SIZE - 24 
        
        for i in range(0, history_limit, 2):
            try:
                candidate = prices[i : i + PATTERN_SIZE]
                candidate_scaler = MinMaxScaler()
                candidate_norm = candidate_scaler.fit_transform(candidate.reshape(-1, 1)).flatten()
                
                corr, _ = pearsonr(current_norm, candidate_norm)
                
                if corr > 0.85: 
                    # 🚀 NORMALIZATION LOGIC
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
    except: return []

# --- 4. TRADE SETUP (SMC BASED) ---
def calculate_trade_levels(current_price, signal, support, resistance, atr_value=None):
    try:
        if atr_value is None or atr_value == 0: atr_value = current_price * 0.01 
        
        entry = current_price
        if signal == "BUY":
            # SL just below the Bullish Order Block
            stop_loss = support if (support > 0 and support < entry) else current_price - (atr_value * 1.5)
            # TP at the next Bearish Liquidity Pool
            take_profit = resistance if (resistance > entry) else current_price + (abs(entry - stop_loss) * 2)
        elif signal == "SELL":
            stop_loss = resistance if (resistance > 0 and resistance > entry) else current_price + (atr_value * 1.5)
            take_profit = support if (support < entry and support > 0) else current_price - (abs(entry - stop_loss) * 2)
        else:
            return None
        
        return {
            "entry": round(entry, 2),
            "stop_loss": round(stop_loss, 2),
            "take_profit": round(take_profit, 2),
            "risk_reward": round(abs(take_profit - entry) / max(0.01, abs(entry - stop_loss)), 2)
        }
    except: return None

# --- 5. API ENDPOINT ---
@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    df = MARKET_MEMORY["df"]
    if df is None or df.empty: return {"signal": "HOLD", "confidence": 0}

    try:
        current_price = safe_float(df['Close'].iloc[-1])
        ema_200 = safe_float(EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1], current_price)
        rsi = safe_float(RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1], 50.0)
        atr = safe_float((df['High'] - df['Low']).tail(14).mean(), current_price * 0.01)
        trend = "UPTREND" if current_price > ema_200 else "DOWNTREND"
        
        # --- EXECUTE ENGINES ---
        smc_zones = detect_smc_structures(df) # 🆕 SMC Engine
        matches = find_fractals(df)           # 🆕 Normalized Fractals
        
        # --- FILTER ZONES ---
        # Find nearest Bullish Zone (Support) and Bearish Zone (Resistance)
        bullish_zones = [z['price'] for z in smc_zones if z['type'] in ['OB_BULL', 'FVG_BULL'] and z['price'] < current_price]
        bearish_zones = [z['price'] for z in smc_zones if z['type'] in ['OB_BEAR', 'FVG_BEAR'] and z['price'] > current_price]
        
        sup_level = max(bullish_zones) if bullish_zones else current_price * 0.98
        res_level = min(bearish_zones) if bearish_zones else current_price * 1.02

        # Convert to legacy "lines" format for frontend compatibility
        trendlines = []
        if res_level: trendlines.append({"type": "RESISTANCE", "price": res_level})
        if sup_level: trendlines.append({"type": "SUPPORT", "price": sup_level})

        # --- SIGNAL LOGIC ---
        total_matches = len(matches)
        signal = "NEUTRAL"
        confidence = 0
        best_match = matches[0] if matches else None

        if total_matches >= 2:
            matches.sort(key=lambda x: x['similarity'], reverse=True)
            bull_votes = len([m for m in matches if m['outcome'] == "BULLISH"])
            bear_votes = len([m for m in matches if m['outcome'] == "BEARISH"])
            
            if bull_votes > bear_votes:
                signal = "BUY"
                confidence = (bull_votes / total_matches) * 100
            elif bear_votes > bull_votes:
                signal = "SELL"
                confidence = (bear_votes / total_matches) * 100
            
            if (signal == "BUY" and trend == "UPTREND") or (signal == "SELL" and trend == "DOWNTREND"): confidence += 10
            if (signal == "BUY" and rsi > 70) or (signal == "SELL" and rsi < 30): confidence -= 15

        trade_setup = calculate_trade_levels(current_price, signal, sup_level, res_level, atr)

        return {
            "signal": signal,
            "confidence": int(max(0, min(99, confidence))),
            "trend": trend,
            "pattern": f"SMC Scan ({len(smc_zones)} Zones)",
            "reasoning": [
                f"Market Structure: {trend}",
                f"Nearest OB Support: {round(sup_level, 2)}",
                f"Nearest OB Resistance: {round(res_level, 2)}"
            ],
            "keyLevels": {"resistance": res_level, "support": sup_level, "ema": ema_200},
            "visuals": {"lines": trendlines, "fractal": best_match},
            "tradeSetup": trade_setup
        }
    except Exception as e:
        logger.error(f"Analysis Crash: {e}", exc_info=True)
        return {"signal": "ERROR", "confidence": 0}

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

@app.get("/health")
async def health():
    return {"status": "ok", "candles": len(MARKET_MEMORY["df"]) if MARKET_MEMORY["df"] is not None else 0}