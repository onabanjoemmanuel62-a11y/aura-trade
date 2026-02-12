import os
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sklearn.preprocessing import MinMaxScaler
from scipy.stats import pearsonr
import logging

# ✅ CORRECT IMPORTS
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator 

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ⚙️ SETTINGS
CSV_FILENAME = "1h.csv"  
PATTERN_SIZE = 60       

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

class AnalysisRequest(BaseModel):
    timeframe: str = "1h"
    currency: str = "USD"

# --- 1. DATA ENGINE (CSV MODE) ---
def fetch_local_data():
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(base_dir, CSV_FILENAME)

        logger.info(f"📂 Loading local database from: {file_path}")

        if not os.path.exists(file_path):
            if os.path.exists(CSV_FILENAME):
                file_path = CSV_FILENAME
            else:
                raise FileNotFoundError(f"Database file {CSV_FILENAME} not found!")

        # 📖 READ CSV (ROBUST MODE)
        try:
            # Try reading with semi-colon
            df = pd.read_csv(file_path, sep=';')
            if len(df.columns) < 2:
                df = pd.read_csv(file_path, sep=',')
        except Exception:
            df = pd.read_csv(file_path, sep=',')
        
        # Normalize Column Names
        df.columns = [c.lower().strip() for c in df.columns]
        
        rename_map = {
            'close': 'Close', 'c': 'Close', '<close>': 'Close',
            'high': 'High', 'h': 'High', '<high>': 'High',
            'low': 'Low', 'l': 'Low', '<low>': 'Low',
            'open': 'Open', 'o': 'Open', '<open>': 'Open',
            'date': 'Date', 'time': 'Date', 'timestamp': 'Date', '<date>': 'Date'
        }
        df.rename(columns=rename_map, inplace=True)
        
        # ⚠️ CRITICAL FIX: CLEAN THE NUMBERS
        # This converts "1,234" (Text) -> 1234.0 (Number)
        numeric_cols = ['Open', 'High', 'Low', 'Close']
        for col in numeric_cols:
            if col in df.columns:
                # 1. Force to string first
                df[col] = df[col].astype(str)
                # 2. Replace comma with dot (if European format)
                df[col] = df[col].str.replace(',', '.')
                # 3. Convert to Number (Coerce errors to NaN)
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # Drop rows where conversion failed (removes bad data)
        df.dropna(subset=numeric_cols, inplace=True)

        # Handle Date Index
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'])
            df.set_index('Date', inplace=True)

        logger.info(f"✅ Loaded {len(df)} candles from local database.")
        return df

    except Exception as e:
        logger.error(f"❌ Database Load Error: {e}")
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

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
    
    # Optimization: Scan every 2nd candle
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
    try:
        df = fetch_local_data()
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
            "pattern": f"Deep Scan ({total_matches} Hist. Matches)",
            "reasoning": [
                f"Analyzed internal database ({len(df)} candles).",
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