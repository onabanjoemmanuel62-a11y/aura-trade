import pandas as pd
import numpy as np
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from scipy.signal import argrelextrema
import warnings
warnings.filterwarnings('ignore')

print("🚀 Starting AuraTrade AI Data Harvester...")

# ==========================================
# 1. LOAD & CLEAN DATA
# ==========================================
print("📦 Loading 1h.csv (Gold Data)...")
try:
    try:
        gold_df = pd.read_csv('1h.csv', sep=';')
        if len(gold_df.columns) < 2: 
            gold_df = pd.read_csv('1h.csv', sep=',')
    except:
        gold_df = pd.read_csv('1h.csv', sep=',')
        
    gold_df.columns = [c.lower().strip() for c in gold_df.columns]
    
    rename_map = {'timestamp': 'date', 'time': 'date', 'datetime': 'date', 'local time': 'date', 'date/time': 'date'}
    gold_df.rename(columns=rename_map, inplace=True)
    
    if 'date' not in gold_df.columns:
        print("❌ CRITICAL ERROR: Could not find a time/date column in Gold data.")
        exit()
        
    gold_df['date'] = pd.to_datetime(gold_df['date'], errors='coerce')
    gold_df.dropna(subset=['date', 'close', 'high', 'low', 'open'], inplace=True)
    gold_df.sort_values('date', inplace=True)
    gold_df.reset_index(drop=True, inplace=True)

    print(f"✅ Loaded {len(gold_df)} 1H Gold Candles.")
except Exception as e:
    print(f"❌ Error loading 1h.csv: {e}")
    exit()

print("🧮 Calculating Technical Indicators...")
gold_df['ema_50'] = EMAIndicator(close=gold_df['close'], window=50).ema_indicator()
gold_df['ema_200'] = EMAIndicator(close=gold_df['close'], window=200).ema_indicator()
gold_df['rsi'] = RSIIndicator(close=gold_df['close'], window=14).rsi()
gold_df['tr'] = np.maximum(gold_df['high'] - gold_df['low'], np.abs(gold_df['high'] - gold_df['close'].shift(1)))
gold_df['atr'] = gold_df['tr'].rolling(14).mean()
gold_df.dropna(inplace=True)
gold_df.reset_index(drop=True, inplace=True)

print("📰 Processing scrape.csv (Forex Factory News)...")
try:
    try:
        news_df = pd.read_csv('scrape.csv', sep=';')
        if len(news_df.columns) < 2: 
            news_df = pd.read_csv('scrape.csv', sep=',')
    except:
        news_df = pd.read_csv('scrape.csv', sep=',')
        
    news_df.columns = [c.lower().strip() for c in news_df.columns]
    
    # 👈 HERE IS THE FIX: Map 'datetime' correctly
    if 'datetime' in news_df.columns:
        news_df.rename(columns={'datetime': 'date'}, inplace=True)
    elif 'time' in news_df.columns:
        news_df.rename(columns={'time': 'date'}, inplace=True)
        
    usd_news = news_df[(news_df['currency'].astype(str).str.upper() == 'USD') & 
                       (news_df['impact'].astype(str).str.contains('High|Red', na=False, case=False))]
    
    usd_news['parsed_date'] = pd.to_datetime(usd_news['date'], errors='coerce')
    usd_news['actual_val'] = pd.to_numeric(usd_news['actual'].astype(str).str.replace(r'[^\d.-]', '', regex=True), errors='coerce')
    usd_news['forecast_val'] = pd.to_numeric(usd_news['forecast'].astype(str).str.replace(r'[^\d.-]', '', regex=True), errors='coerce')
    
    usd_news['news_bias'] = np.where(usd_news['actual_val'] < usd_news['forecast_val'], 1, 
                            np.where(usd_news['actual_val'] > usd_news['forecast_val'], -1, 0))
    
    daily_news_bias = usd_news.groupby(usd_news['parsed_date'].dt.date)['news_bias'].sum().to_dict()
    print(f"✅ Loaded {len(usd_news)} High-Impact USD News Events.")
except Exception as e:
    print(f"⚠️ News processing skipped or failed: {e}")
    daily_news_bias = {}

# ==========================================
# 2. SMC EXTRACTION & LABELING ENGINE
# ==========================================
print("🔎 Scanning Market Structure for High-Probability Order Blocks...")
dataset = []

highs = gold_df['high'].values
lows = gold_df['low'].values
closes = gold_df['close'].values
opens = gold_df['open'].values
dates = gold_df['date'].values
ema50 = gold_df['ema_50'].values
ema200 = gold_df['ema_200'].values
rsi = gold_df['rsi'].values
atr = gold_df['atr'].values

swing_period = 5
swing_highs = argrelextrema(highs, np.greater, order=swing_period)[0]
swing_lows = argrelextrema(lows, np.less, order=swing_period)[0]

all_swings = sorted(list(set(swing_highs) | set(swing_lows)))

for i in range(10, len(all_swings) - 2):
    ob_idx = all_swings[i]
    if ob_idx + 10 >= len(highs): continue 

    is_bullish_ob = ob_idx in swing_lows
    is_bearish_ob = ob_idx in swing_highs

    if is_bearish_ob and closes[ob_idx] < opens[ob_idx] and closes[ob_idx-1] > opens[ob_idx-1]:
        ob_idx = ob_idx - 1
    elif is_bullish_ob and closes[ob_idx] > opens[ob_idx] and closes[ob_idx-1] < opens[ob_idx-1]:
        ob_idx = ob_idx - 1

    # ✅ FIX — everything gets 4 spaces so it's inside the loop
    ob_top = highs[ob_idx]
    ob_bottom = lows[ob_idx]
    
    c1 = ob_idx + 1
    c3 = ob_idx + 3
    if c3 < len(lows):
        has_bullish_fvg = lows[c3] > highs[c1]
        has_bearish_fvg = highs[c3] < lows[c1]
    else:
        has_bullish_fvg = False
        has_bearish_fvg = False
        
    if is_bullish_ob and not has_bullish_fvg: continue
    if is_bearish_ob and not has_bearish_fvg: continue

    momentum = abs(closes[ob_idx+2] - opens[ob_idx])
    current_atr = atr[ob_idx]
    
    if momentum < current_atr: continue 

    mitigation_idx = None
    for fut in range(ob_idx + 3, len(highs)):
        if is_bullish_ob and lows[fut] <= ob_top:
            mitigation_idx = fut
            break
        if is_bearish_ob and highs[fut] >= ob_bottom:
            mitigation_idx = fut
            break
            
    if mitigation_idx is None: continue 
    
    trend = 1 if ema50[mitigation_idx] > ema200[mitigation_idx] else -1
    
    if is_bullish_ob and trend == -1: continue
    if is_bearish_ob and trend == 1: continue

    sl = ob_bottom - (current_atr * 0.2) if is_bullish_ob else ob_top + (current_atr * 0.2)
    entry_price = ob_top if is_bullish_ob else ob_bottom
    risk = abs(entry_price - sl)
    tp = entry_price + (risk * 2.0) if is_bullish_ob else entry_price - (risk * 2.0)

    label = 0 
    for sim_idx in range(mitigation_idx + 1, min(mitigation_idx + 300, len(highs))):
        c_high = highs[sim_idx]
        c_low = lows[sim_idx]
        
        if is_bullish_ob:
            if c_low <= sl: 
                label = 0; break 
            if c_high >= tp: 
                label = 1; break 
        else:
            if c_high >= sl: 
                label = 0; break 
            if c_low <= tp: 
                label = 1; break 

    date_val = pd.to_datetime(dates[mitigation_idx])
    daily_bias = daily_news_bias.get(date_val.date(), 0)

    dataset.append({
        "date": date_val,
        "type": 1 if is_bullish_ob else 0, 
        "fvg_size_pips": round(abs(lows[c3] - highs[c1]) if is_bullish_ob else abs(lows[c1] - highs[c3]), 2),
        "rsi_at_entry": round(rsi[mitigation_idx], 2),
        "atr_at_entry": round(atr[mitigation_idx], 2),
        "momentum_ratio": round(momentum / current_atr, 2),
        "news_bias": daily_bias, 
        "LABEL_WIN": label 
    })

# ==========================================
# 3. SAVE THE DATASET
# ==========================================
final_df = pd.DataFrame(dataset)

if len(final_df) > 0:
    final_df.to_csv("smc_training_data.csv", index=False)
    print("\n✅ DATA HARVEST COMPLETE!")
    print(f"Total SMC Setups Simulated: {len(final_df)}")
    print(f"Total Winning Trades (Label 1): {len(final_df[final_df['LABEL_WIN'] == 1])}")
    print(f"Total Losing Trades (Label 0): {len(final_df[final_df['LABEL_WIN'] == 0])}")
    print("Saved to: smc_training_data.csv")
else:
    print("\n❌ No setups found. Check if your CSV dates/formats are mapping correctly.")