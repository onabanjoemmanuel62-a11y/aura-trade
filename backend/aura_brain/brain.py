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
    logger.warning(f"⚠️ ML Brain not found. Falling back to rule-based confidence. Error: {e}")

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
        logger.error(f"❌ CSV Load Failed: {e}")
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
        logger.error(f"❌ Live Data Conversion Failed: {e}")
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
# 🧠 UPGRADED SMC ENGINE: ALGORITHMIC NOISE REDUCTION
# =================================================================
def detect_smc_structures(df):
    zones = []
    raw_lines = [] 
    try:
        if 'Date' in df.columns:
            dates = df['Date'].values
        else:
            dates = df.index.values

        opens = df['Open'].values
        closes = df['Close'].values
        highs = df['High'].values
        lows = df['Low'].values
        
        tr = np.maximum(highs - lows, np.abs(highs - np.roll(closes, 1)))
        atr = pd.Series(tr).rolling(14).mean().bfill().values

        swing_period = 5
        swing_highs = argrelextrema(highs, np.greater, order=swing_period)[0]
        swing_lows = argrelextrema(lows, np.less, order=swing_period)[0]

        # --- A. BEARISH OB (Supply) ---
        for idx in swing_highs[-40:]: 
            ob_idx = idx
            if closes[ob_idx] < opens[ob_idx] and ob_idx > 0 and closes[ob_idx-1] > opens[ob_idx-1]:
                ob_idx = ob_idx - 1 
                
            if ob_idx + 3 >= len(highs): continue 

            prior_lows = [l for l in swing_lows if l < ob_idx]
            if not prior_lows: continue
            target_low_idx = prior_lows[-1]
            bos_level = lows[target_low_idx]
            
            line_label = "BOS"
            if len(prior_lows) >= 2:
                if lows[target_low_idx] >= lows[prior_lows[-2]]:
                    line_label = "CHoCH"
            
            subsequent_lows = lows[ob_idx+1:]
            if len(subsequent_lows) == 0: continue

            if np.min(subsequent_lows) < bos_level: 
                break_idx = None
                for i in range(ob_idx + 1, len(lows)):
                    if lows[i] < bos_level:
                        break_idx = i
                        break

                has_fvg = highs[ob_idx+2] < lows[ob_idx]
                momentum = abs(highs[ob_idx] - closes[ob_idx+2])
                has_momentum = momentum > atr[ob_idx]
                
                if not (has_fvg and has_momentum):
                    continue 
                
                ob_top = highs[ob_idx]
                ob_bottom = lows[ob_idx]
                ob_time = dates[ob_idx] 
                
                is_tested = False
                mitigated_time = None
                
                for future_idx in range(ob_idx + 2, len(highs)):
                    if highs[future_idx] >= ob_bottom:
                        is_tested = True
                        mitigated_time = dates[future_idx]
                        break

                if is_tested and (highs[-1] > ob_top): 
                    continue 

                if break_idx:
                    raw_lines.append({
                        "level": float(bos_level),
                        "start_time": int(dates[target_low_idx]),
                        "end_time": int(dates[break_idx]),
                        "type": line_label, 
                        "color": "rgba(239, 83, 80, 0.8)" 
                    })
                
                zones.append({
                    "type": "OB_BEAR", 
                    "top": float(ob_top), 
                    "bottom": float(ob_bottom), 
                    "price": float(ob_bottom), 
                    "time": int(ob_time),
                    "mitigated_time": int(mitigated_time) if is_tested else None,
                    "is_mitigated": is_tested,
                    "fvg_size_pips": float(abs(lows[ob_idx] - highs[ob_idx+2])),
                    "momentum_ratio": float(momentum / atr[ob_idx])
                })

        # --- B. BULLISH OB (Demand) ---
        for idx in swing_lows[-40:]:
            ob_idx = idx
            if closes[ob_idx] > opens[ob_idx] and ob_idx > 0 and closes[ob_idx-1] < opens[ob_idx-1]:
                ob_idx = ob_idx - 1 
                
            if ob_idx + 3 >= len(lows): continue 
            
            prior_highs = [h for h in swing_highs if h < ob_idx]
            if not prior_highs: continue
            target_high_idx = prior_highs[-1]
            bos_level = highs[target_high_idx]

            line_label = "BOS"
            if len(prior_highs) >= 2:
                if highs[target_high_idx] <= highs[prior_highs[-2]]:
                    line_label = "CHoCH"

            subsequent_highs = highs[ob_idx+1:]
            if len(subsequent_highs) == 0: continue

            if np.max(subsequent_highs) > bos_level: 
                break_idx = None
                for i in range(ob_idx + 1, len(highs)):
                    if highs[i] > bos_level:
                        break_idx = i
                        break

                has_fvg = lows[ob_idx+2] > highs[ob_idx]
                momentum = abs(closes[ob_idx+2] - lows[ob_idx])
                has_momentum = momentum > atr[ob_idx]
                
                if not (has_fvg and has_momentum):
                    continue 
                
                ob_top = highs[ob_idx]
                ob_bottom = lows[ob_idx]
                ob_time = dates[ob_idx] 
                
                is_tested = False
                mitigated_time = None
                
                for future_idx in range(ob_idx + 2, len(lows)):
                    if lows[future_idx] <= ob_top:
                        is_tested = True
                        mitigated_time = dates[future_idx]
                        break

                if is_tested and (lows[-1] < ob_bottom): 
                    continue 

                if break_idx:
                    raw_lines.append({
                        "level": float(bos_level),
                        "start_time": int(dates[target_high_idx]),
                        "end_time": int(dates[break_idx]),
                        "type": line_label, 
                        "color": "rgba(38, 166, 154, 0.8)" 
                    })
                
                zones.append({
                    "type": "OB_BULL", 
                    "top": float(ob_top), 
                    "bottom": float(ob_bottom), 
                    "price": float(ob_top), 
                    "time": int(ob_time),
                    "mitigated_time": int(mitigated_time) if is_tested else None,
                    "is_mitigated": is_tested,
                    "fvg_size_pips": float(abs(lows[ob_idx+2] - highs[ob_idx])),
                    "momentum_ratio": float(momentum / atr[ob_idx])
                })

        bull_zones = sorted([z for z in zones if z['type'] == 'OB_BULL'], key=lambda x: x['time'], reverse=True)[:2]
        bear_zones = sorted([z for z in zones if z['type'] == 'OB_BEAR'], key=lambda x: x['time'], reverse=True)[:2]
        clean_zones = bull_zones + bear_zones

        clean_lines = []
        seen_levels = set()
        for line in reversed(raw_lines): 
            level_rounded = round(line['level'], 1)
            if level_rounded not in seen_levels:
                clean_lines.append(line)
                seen_levels.add(level_rounded)

        return {"zones": clean_zones, "lines": clean_lines} 
    except Exception as e:
        logger.warning(f"SMC Error: {e}")
        return {"zones": [], "lines": []}

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
        
        ema_50 = safe_float(EMAIndicator(close=df['Close'], window=50).ema_indicator().iloc[-1], current_price)
        ema_200 = safe_float(EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1], current_price)
        rsi = safe_float(RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1], 50.0)
        atr = safe_float((df['High'] - df['Low']).tail(14).mean(), current_price * 0.01)
        
        if ema_50 > ema_200 and current_price > ema_200:
            ltf_trend = "UPTREND"
        elif ema_50 < ema_200 and current_price < ema_200:
            ltf_trend = "DOWNTREND"
        else:
            ltf_trend = "RANGING"

        htf_trend = ltf_trend 
        if req.htf_candles and len(req.htf_candles) > 50:
            df_htf = process_live_candles(req.htf_candles)
            if df_htf is not None and not df_htf.empty:
                htf_ema_50 = safe_float(EMAIndicator(close=df_htf['Close'], window=50).ema_indicator().iloc[-1], current_price)
                htf_ema_200 = safe_float(EMAIndicator(close=df_htf['Close'], window=200).ema_indicator().iloc[-1], current_price)
                if htf_ema_50 > htf_ema_200:
                    htf_trend = "UPTREND"
                elif htf_ema_50 < htf_ema_200:
                    htf_trend = "DOWNTREND"
                else:
                    htf_trend = "RANGING"
        
        smc_data = detect_smc_structures(df) 
        smc_zones = smc_data.get("zones", [])
        bos_lines = smc_data.get("lines", [])
        
        bullish_zones = sorted([z for z in smc_zones if z['type'] == 'OB_BULL' and not z['is_mitigated'] and z['top'] < current_price], key=lambda x: current_price - x['top'])
        bearish_zones = sorted([z for z in smc_zones if z['type'] == 'OB_BEAR' and not z['is_mitigated'] and z['bottom'] > current_price], key=lambda x: x['bottom'] - current_price)
        
        nearest_supp = bullish_zones[0] if bullish_zones else None
        nearest_res = bearish_zones[0] if bearish_zones else None
        
        sup_level = nearest_supp['top'] if nearest_supp else current_price * 0.985
        res_level = nearest_res['bottom'] if nearest_res else current_price * 1.015

        highs_arr = df['High'].values
        lows_arr = df['Low'].values
        
        conf_swing_highs = argrelextrema(highs_arr, np.greater, order=5)[0]
        conf_swing_lows = argrelextrema(lows_arr, np.less, order=5)[0]
        
        if len(conf_swing_highs) > 0:
            buy_side_liquidity = float(np.max(highs_arr[conf_swing_highs[-3:]]))
        else:
            buy_side_liquidity = float(np.max(highs_arr[-50:-5])) 
            
        if len(conf_swing_lows) > 0:
            sell_side_liquidity = float(np.min(lows_arr[conf_swing_lows[-3:]]))
        else:
            sell_side_liquidity = float(np.min(lows_arr[-50:-5]))

        recent_candles = df.tail(3)
        liquidity_sweep_bullish = False
        liquidity_sweep_bearish = False
        
        if nearest_supp:
            for _, c in recent_candles.iterrows():
                if c['Low'] < nearest_supp['bottom'] and c['Close'] > nearest_supp['bottom']:
                    liquidity_sweep_bullish = True
                    
        if nearest_res:
            for _, c in recent_candles.iterrows():
                if c['High'] > nearest_res['top'] and c['Close'] < nearest_res['top']:
                    liquidity_sweep_bearish = True

        news_bias = "NEUTRAL"
        news_val = 0
        news_string = "No recent impactful news data."
        
        if req.news_data:
            actual = safe_float(req.news_data.get('actual', 0))
            forecast = safe_float(req.news_data.get('forecast', 0))
            event_name = req.news_data.get('event', 'News Event')
            
            if actual > forecast:
                news_bias = "BEARISH_GOLD"
                news_val = -1
                news_string = f"📰 {event_name}: Actual ({actual}) beat Forecast ({forecast}). Strong USD limits Gold."
            elif actual < forecast:
                news_bias = "BULLISH_GOLD"
                news_val = 1
                news_string = f"📰 {event_name}: Actual ({actual}) missed Forecast ({forecast}). Weak USD fuels Gold."

        signal = "NEUTRAL"
        confidence = 0
        base_conf = 0
        target_zone = None
        
        # 🔥 CRITICAL FIX: The clean Master Bias logic!
        trend_aligned = (ltf_trend == htf_trend)
        master_bias = "NEUTRAL"
        
        if trend_aligned:
            master_bias = ltf_trend
        elif htf_trend == "UPTREND" and ltf_trend == "DOWNTREND":
            master_bias = "PULLBACK (Waiting to Buy)"
        elif htf_trend == "DOWNTREND" and ltf_trend == "UPTREND":
            master_bias = "PULLBACK (Waiting to Sell)"
        else:
            master_bias = htf_trend

        strategy_logic = [f"🧭 Master Bias: {master_bias}", news_string]

        if ltf_trend == "UPTREND" and nearest_supp:
            strategy_logic.append(f"🎯 Target Liquidity (BSL): {round(buy_side_liquidity, 2)}")
            distance_to_ob = current_price - nearest_supp['top']
            
            if distance_to_ob <= (atr * 3): 
                if not trend_aligned and htf_trend == "DOWNTREND":
                    strategy_logic.append("🚫 Matrix Block: Ignoring 1H Buy against 4H Bearish Macro.")
                else:
                    signal = "BUY"
                    target_zone = nearest_supp
                    base_conf = 55
                    
                    if current_price <= nearest_supp['top'] and current_price >= nearest_supp['bottom']:
                        base_conf += 20 
                        strategy_logic.append("Premium Entry: Price inside High-Prob Bullish OB.")
                    elif distance_to_ob <= atr:
                        base_conf += 10
                        strategy_logic.append("Approaching Entry: Price near Bullish OB.")
                    else:
                        strategy_logic.append("Waiting for retracement into Order Block.")
                        
                    if liquidity_sweep_bullish:
                        base_conf += 15
                        strategy_logic.append("🚨 Bullish Stop Hunt Detected! Early sellers trapped.")
                    
                    if news_bias == "BULLISH_GOLD":
                        base_conf += 15
                        strategy_logic.append("🔥 News Aligns with Trend! Massive Bullish Confluence.")
                    elif news_bias == "BEARISH_GOLD":
                        base_conf -= 25
                        strategy_logic.append("⚠️ WARNING: Strong USD News contradicts technical setup.")

                    if rsi < 45:
                        base_conf += 8
                        strategy_logic.append("RSI favors an upward bounce.")
                        
                    confidence = min(99, base_conf)
            else:
                strategy_logic.append(f"Price is too far from support ({round(nearest_supp['top'], 2)}). Ignoring Sells against trend.")

        elif ltf_trend == "DOWNTREND" and nearest_res:
            strategy_logic.append(f"🎯 Target Liquidity (SSL): {round(sell_side_liquidity, 2)}")
            distance_to_ob = nearest_res['bottom'] - current_price
            
            if distance_to_ob <= (atr * 3):
                if not trend_aligned and htf_trend == "UPTREND":
                    strategy_logic.append("🚫 Matrix Block: Ignoring 1H Sell against 4H Bullish Macro.")
                else:
                    signal = "SELL"
                    target_zone = nearest_res
                    base_conf = 55
                    
                    if current_price >= nearest_res['bottom'] and current_price <= nearest_res['top']:
                        base_conf += 20
                        strategy_logic.append("Premium Entry: Price inside High-Prob Bearish OB.")
                    elif distance_to_ob <= atr:
                        base_conf += 10
                        strategy_logic.append("Approaching Entry: Price near Bearish OB.")
                    else:
                        strategy_logic.append("Waiting for rally into Order Block.")
                        
                    if liquidity_sweep_bearish:
                        base_conf += 15
                        strategy_logic.append("🚨 Bearish Stop Hunt Detected! Early buyers trapped.")
                        
                    if news_bias == "BEARISH_GOLD":
                        base_conf += 15
                        strategy_logic.append("🔥 News Aligns with Trend! Massive Bearish Confluence.")
                    elif news_bias == "BULLISH_GOLD":
                        base_conf -= 25
                        strategy_logic.append("⚠️ WARNING: Weak USD News contradicts technical setup.")

                    if rsi > 55:
                        base_conf += 8
                        strategy_logic.append("RSI favors a downward rejection.")
                        
                    confidence = min(99, base_conf)
            else:
                strategy_logic.append(f"Price is too far from resistance ({round(nearest_res['bottom'], 2)}). Ignoring Buys against trend.")
        else:
            strategy_logic.append("No clear institutional setups in current range.")

        # =========================================================
        # 🧠 ML NEURAL PREDICTION OVERRIDE
        # =========================================================
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
                confidence = int(prob * 100)
                
                strategy_logic.append(f"🧠 ML Neural Prediction: {confidence}% Win Probability")
            except Exception as e:
                logger.error(f"ML Prediction Failed: {e}")

        trade_setup = calculate_trade_levels(current_price, signal, sup_level, res_level, atr)

        return {
            "signal": signal,
            "confidence": int(confidence),
            "trend": master_bias, # 👈 Outputs the clean Master Bias to the React UI
            "pattern": f"Strict SMC + Matrix Align ({data_source})",
            "reasoning": strategy_logic,
            "keyLevels": {"resistance": res_level, "support": sup_level, "ema": ema_200},
            "visuals": {
                "smc_zones": smc_zones, 
                "bos_lines": bos_lines 
            },
            "tradeSetup": trade_setup if confidence >= 60 else None
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