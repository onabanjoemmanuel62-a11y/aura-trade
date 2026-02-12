import os
# 🔧 FIX: Tell yfinance to use a temporary cache folder (Render is Read-Only)
os.environ['YFINANCE_CACHE_DIR'] = '/tmp/yfinance'

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yfinance as yf
import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from scipy.stats import pearsonr
import uvicorn
import logging

app = FastAPI()

# --- CORS: Allow your Vercel Frontend to access this ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SETTINGS
SYMBOL = "GC=F"       # Gold Futures
HISTORY_PERIOD = "10y" # Scan 10 Years
INTERVAL = "1h"       
PATTERN_SIZE = 60     

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

class AnalysisRequest(BaseModel):
    timeframe: str = "1h"
    currency: str = "USD"

# --- 1. DATA ENGINE ---
def fetch_market_data():
    logger.info(f"Downloading {HISTORY_PERIOD} of data for {SYMBOL}...")
    try:
        df = yf.download(SYMBOL, period=HISTORY_PERIOD, interval=INTERVAL, progress=False)
        if df.empty:
            raise ValueError("No data returned from Yahoo Finance")
        # Flatten MultiIndex columns if present (Fix for new yfinance versions)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df[['Close', 'High', 'Low']]
        df.dropna(inplace=True)
        return df
    except Exception as e:
        logger.error(f"Data Download Error: {e}")
        raise HTTPException(status_code=503, detail="Market Data Service Unavailable")

# --- 2. FRACTAL PATTERN RECOGNITION ---
def find_fractals(df):
    prices = df['Close'].values
    if len(prices) < PATTERN_SIZE + 24:
        return []

    current_pattern = prices[-PATTERN_SIZE:]
    scaler = MinMaxScaler()
    current_norm = scaler.fit_transform(current_pattern.reshape(-1, 1)).flatten()

    matches = []
    history_limit = len(prices) - PATTERN_SIZE - 24 

    # Scan history with a stride of 3 for density
    for i in range(0, history_limit, 3):
        candidate = prices[i : i + PATTERN_SIZE]
        candidate_norm = scaler.fit_transform(candidate.reshape(-1, 1)).flatten()

        corr, _ = pearsonr(current_norm, candidate_norm)

        if corr > 0.82: # 82% Similarity Threshold
            future_price = prices[i + PATTERN_SIZE + 24]
            entry_price = prices[i + PATTERN_SIZE]
            outcome = "BULLISH" if future_price > entry_price else "BEARISH"
            matches.append({"outcome": outcome, "similarity": corr})

    return matches

# --- 3. API ENDPOINT ---
@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    try:
        df = fetch_market_data()
        current_price = df['Close'].iloc[-1]

        # Technicals
        ema_200 = EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1]
        rsi = RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1]
        trend = "UPTREND" if current_price > ema_200 else "DOWNTREND"

        # Fractals
        matches = find_fractals(df)
        total_matches = len(matches)

        # Default "Safe" State
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

            # Logic Penalties
            if (signal == "BUY" and trend == "DOWNTREND") or (signal == "SELL" and trend == "UPTREND"):
                confidence -= 15
            if (signal == "BUY" and rsi > 70) or (signal == "SELL" and rsi < 30):
                confidence -= 10

        return {
            "signal": signal,
            "confidence": int(max(0, min(99, confidence))),
            "trend": trend,
            "pattern": f"Fractal ({total_matches} Hist. Matches)",
            "reasoning": [
                f"AI scanned 10 years of market history.",
                f"Identified {total_matches} similar fractal structures.",
                f"Trend is currently {trend}."
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