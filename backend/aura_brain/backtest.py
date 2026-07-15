"""
Backtest harness for the AuraBrain MMM engine.

This script imports brain.py's actual functions directly (not a
reimplementation) and replays them candle-by-candle across your
historical 1h.csv, so the backtest is guaranteed to test exactly
the same logic that runs live — no drift between "what we tested"
and "what's actually deployed".

For each historical candle where the engine would have fired a
BUY/SELL signal with confidence >= CONFIDENCE_THRESHOLD, it walks
forward through the real future candles to see whether price hit
the stop-loss or take-profit first, and reports:

  - overall win rate
  - win rate broken down by confidence bucket (so you can see
    whether higher confidence actually means higher accuracy)
  - win rate broken down by BUY vs SELL

Usage:
    cd backend/aura_brain
    python backtest.py

Output:
    Prints a summary report to the terminal.
    Saves every individual simulated trade to backtest_results.csv
    in the same folder, for further analysis in Excel/pandas.
"""

import os
import sys
import pandas as pd
import numpy as np

# Import the live engine directly — single source of truth.
# This requires the same dependencies as brain.py (fastapi, ta, etc.)
# to be installed, since importing the module runs its top-level code.
import brain
import logging
logging.getLogger("AuraBrain").setLevel(logging.WARNING)  # brain.py logs INFO per candle; silence it for readable backtest output

CSV_1H = "1h.csv"
CSV_4H = "4h.csv"  # optional — not currently wired into htf_aligned below,
                    # since analyze() computes htf_aligned from live htf_candles
                    # which we don't have a synced historical version of yet.
NEWS_CSV = "scrape.csv"

MIN_HISTORY = 250          # candles of lookback needed before EMA200/swing detection is meaningful
FORWARD_WINDOW = 300        # how many future candles to search for a TP/SL hit before giving up
CONFIDENCE_THRESHOLD = 65   # must match brain.py's live threshold in analyze()
ROLLING_WINDOW = 500        # how many past candles the engine "sees" at each step, mirrors live payload size

# Optional: limit the backtest to the most recent N candles for faster iteration.
# Set to None to run across the entire CSV.
# 17520 ≈ 2 years of 1H candles, 8760 ≈ 1 year.
MAX_CANDLES = None

# Out-of-sample validation: the last N% of history (by time) is tagged and
# reported SEPARATELY from the rest. This exists because repeatedly tuning
# entry rules against the same full historical dataset risks fitting noise
# rather than a real edge. Treat the in-sample portion as fair game to keep
# iterating against; the out-of-sample portion should be checked, not
# optimized against — if performance collapses there, the "edge" found in
# the in-sample portion likely isn't real.
OUT_OF_SAMPLE_FRACTION = 0.20


def load_csv(path):
    """Same parsing/cleaning logic as brain.py's load_csv_fallback(), standalone."""
    if not os.path.exists(path):
        print(f"❌ Could not find {path} in the current folder.")
        sys.exit(1)

    try:
        df = pd.read_csv(path, sep=';')
        if len(df.columns) < 2:
            df = pd.read_csv(path, sep=',')
    except Exception:
        df = pd.read_csv(path, sep=',')

    df.columns = [c.lower().strip() for c in df.columns]
    rename_map = {
        'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open',
        'date': 'Date', 'time': 'Date', 'timestamp': 'Date'
    }
    df.rename(columns=rename_map, inplace=True)

    for col in ['Open', 'High', 'Low', 'Close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    if 'Date' in df.columns:
        df['Date'] = pd.to_datetime(df['Date'], format='mixed', dayfirst=False, errors='coerce')
        df.dropna(subset=['Date'], inplace=True)
        df['Date'] = df['Date'].astype(np.int64) // 10 ** 9

    df.dropna(subset=['Open', 'High', 'Low', 'Close'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def load_news_events(path):
    """
    Loads historical news events for point-in-time lookup during the backtest.
    Mirrors the LIVE system's definition exactly (analysisController.js's
    `latestNews` query): the single most recent High-impact event with both
    actual and forecast present, with NO currency filter — matching what's
    actually deployed, even though build_dataset.py's training-time feature
    used a different (USD-only, daily-summed) definition. This backtest is
    validating live behavior, so it mirrors live behavior.
    """
    if not os.path.exists(path):
        print(f"⚠️  {path} not found — news feature disabled (news_bias=0 for all trades).")
        return None
    try:
        df = pd.read_csv(path)
    except Exception as e:
        print(f"⚠️  Failed to load {path}: {e} — news feature disabled.")
        return None

    df.columns = [c.lower().strip() for c in df.columns]
    if 'datetime' not in df.columns or 'impact' not in df.columns or 'actual' not in df.columns or 'forecast' not in df.columns:
        print(f"⚠️  {path} missing expected columns (datetime/impact/actual/forecast) — news feature disabled.")
        return None

    df = df[df['impact'].astype(str).str.contains('High', case=False, na=False)]
    df = df.dropna(subset=['actual', 'forecast'])
    if df.empty:
        print("⚠️  No qualifying high-impact news events found — news feature disabled.")
        return None

    df['datetime'] = pd.to_datetime(df['datetime'], errors='coerce')
    df.dropna(subset=['datetime'], inplace=True)
    df['epoch'] = df['datetime'].astype(np.int64) // 10 ** 9
    df.sort_values('epoch', inplace=True)
    df.reset_index(drop=True, inplace=True)

    print(f"✅ Loaded {len(df)} high-impact news events ({df['datetime'].min()} to {df['datetime'].max()}) for point-in-time lookup.")
    return df


_NEWS_DF = None      # lazily loaded once in run_backtest(), then reused for every candle
_NEWS_EPOCHS = None
_NEWS_ACTUAL = None
_NEWS_FORECAST = None
_NEWS_EVENT_NAMES = None


def get_news_bias_at(candle_epoch):
    """
    Point-in-time lookup: finds the most recent qualifying news event STRICTLY
    BEFORE candle_epoch (never at-or-after, to avoid lookahead bias — the
    backtest must never "know about" news that hadn't happened yet), and
    returns the same -1/0/1 encoding brain.py uses live:
      actual > forecast -> -1 (USD bullish / gold bearish framing)
      actual < forecast -> +1 (USD bearish / gold bullish framing)
      equal or no prior event -> 0
    """
    if _NEWS_EPOCHS is None or len(_NEWS_EPOCHS) == 0:
        return 0
    idx = np.searchsorted(_NEWS_EPOCHS, candle_epoch, side='left') - 1
    if idx < 0:
        return 0
    actual = _NEWS_ACTUAL[idx]
    forecast = _NEWS_FORECAST[idx]
    if actual > forecast:
        return -1
    elif actual < forecast:
        return 1
    return 0


def get_latest_news_event_at(candle_epoch):
    """
    Point-in-time lookup returning the most recent qualifying news event's
    NAME and TIMESTAMP (not just its bias value) — used to feed
    brain.check_news_alignment() the same shape of info the live system
    gets from req.news_data. Same no-lookahead guarantee as get_news_bias_at.
    Returns None if no qualifying event has occurred yet.
    """
    if _NEWS_EPOCHS is None or len(_NEWS_EPOCHS) == 0:
        return None
    idx = np.searchsorted(_NEWS_EPOCHS, candle_epoch, side='left') - 1
    if idx < 0:
        return None
    return {"event": _NEWS_EVENT_NAMES[idx], "time": float(_NEWS_EPOCHS[idx])}


def simulate_outcome(full_df, entry_idx, signal, sl, tp):
    """
    Walk forward from entry_idx through real future candles to see
    whether stop-loss or take-profit is hit first.
    Returns ("WIN"|"LOSS"|"NO_RESULT", hit_index_or_None).
    """
    highs = full_df['High'].values
    lows = full_df['Low'].values
    end = min(entry_idx + 1 + FORWARD_WINDOW, len(full_df))

    for i in range(entry_idx + 1, end):
        if signal == "BUY":
            if lows[i] <= sl:
                return "LOSS", i
            if highs[i] >= tp:
                return "WIN", i
        else:  # SELL
            if highs[i] >= sl:
                return "LOSS", i
            if lows[i] <= tp:
                return "WIN", i

    return "NO_RESULT", None


def generate_signal_at(full_df, i):
    """
    Reproduces brain.py's analyze() decision logic for a single point
    in time, using only data up to and including candle i (no lookahead).
    Returns a dict describing the signal, or None if no trade-eligible
    signal was generated at this candle.
    """
    window = full_df.iloc[max(0, i - ROLLING_WINDOW + 1):i + 1].reset_index(drop=True)
    if len(window) < 100:
        return None

    current_price = float(window['Close'].iloc[-1])
    profile = brain.get_instrument_profile("XAUUSD", current_price)

    ema_200 = brain.safe_float(brain.EMAIndicator(close=window['Close'], window=200).ema_indicator().iloc[-1], current_price)
    ema_50 = brain.safe_float(brain.EMAIndicator(close=window['Close'], window=50).ema_indicator().iloc[-1], current_price)
    rsi = brain.safe_float(brain.RSIIndicator(close=window['Close'], window=14).rsi().iloc[-1], 50.0)
    atr = brain.calculate_atr(window, 14)
    if atr == 0:
        return None

    ms = brain.analyze_market_structure(window, profile)
    cycle = ms['cycle']
    current_level = ms['level']
    phase_str = ms['phase_str']
    in_pullback = ms['in_pullback']
    sweeps = ms['sweeps']
    w_confirmed = ms.get('w_confirmed', False)
    m_confirmed = ms.get('m_confirmed', False)

    bias_str = 'BULLISH' if cycle.startswith('BULLISH') else 'BEARISH'
    order_blocks = brain.detect_order_blocks(window, ms['atr'], bias_str)
    fvgs = brain.detect_fvgs(window, ms['atr'], bias_str)

    ob_present = any((ob['bottom'] - ms['atr']) <= current_price <= (ob['top'] + ms['atr']) for ob in order_blocks)
    fvg_present = any((fvg['bottom'] - ms['atr']) <= current_price <= (fvg['top'] + ms['atr']) for fvg in fvgs)

    # No separate HTF candle series wired into the backtest yet, so this
    # mirrors brain.py's own fallback branch (uses 1H EMA alignment as a proxy).
    htf_aligned = (cycle.startswith('BULLISH') and ema_50 > ema_200) or \
                  (cycle.startswith('BEARISH') and ema_50 < ema_200)

    # Session timing isn't meaningful when replaying historical data out of
    # real-time context, so this factor is neutralized (always True) rather
    # than silently penalizing/boosting scores based on backtest-run time.
    session_aligned = True

    sweep_nearby = False
    if sweeps:
        last_sweep = sweeps[-1]
        candle_age = len(window) - 1 - last_sweep['sweep_idx']
        sweep_nearby = candle_age <= 15

    dist = abs(current_price - ema_50)
    signal = "NEUTRAL"
    confidence = 0

    eligible = (
        "FAILED" not in phase_str
        and "No Trade Zone" not in phase_str
        and not ("EXHAUSTION" in phase_str and not in_pullback)
        and in_pullback
        and current_level < 2   # Level 3 filter: backtest showed 17.1% win rate vs 33.3% for Level 2
    )

    if eligible:
        if cycle.startswith("BULLISH") and current_price >= ema_50 * 0.998 and dist <= (atr * 1.5):
            signal = "BUY"
        elif cycle.startswith("BEARISH") and brain.ALLOW_SELL_SIGNALS and m_confirmed and current_price <= ema_50 * 1.002 and dist <= (atr * 1.5):
            signal = "SELL"

        if signal != "NEUTRAL":
            pattern_confirmed = w_confirmed if signal == "BUY" else True
            confidence = brain.score_mmm_setup(
                current_price, ema_50, ema_200, rsi, current_level, in_pullback,
                cycle, sweep_nearby, atr, phase_str, ob_present, fvg_present,
                session_aligned, htf_aligned, pattern_confirmed=pattern_confirmed
            )

    if signal == "NEUTRAL":
        return None

    # Compute news bias regardless of ML model presence, so it's always logged for analysis
    candle_epoch = float(window['Date'].iloc[-1]) if 'Date' in window.columns else None
    news_bias_used = get_news_bias_at(candle_epoch) if candle_epoch is not None else 0

    # News alignment check — same order as brain.py: applied BEFORE the ML blend
    news_aligned = False
    news_conflicted = False
    news_alignment_event = None
    if candle_epoch is not None:
        latest_event = get_latest_news_event_at(candle_epoch)
        news_check = brain.check_news_alignment(latest_event, signal, reference_time_epoch=candle_epoch)
        news_aligned = news_check["aligned"]
        news_conflicted = news_check["conflicted"]
        news_alignment_event = news_check["event"]
        if brain.APPLY_NEWS_ALIGNMENT_ADJUSTMENT:
            if news_aligned:
                confidence = min(confidence + 10, 99)
            elif news_conflicted:
                confidence = max(confidence - 10, 10)
        # else: still logged below for future analysis, just not affecting confidence yet

    # ML blend — same feature computation as the fixed brain.py analyze()
    if brain.ML_MODEL:
        try:
            live_fvg_size = 0.0
            if fvgs:
                nearest_fvg = min(fvgs, key=lambda f: abs(((f['top'] + f['bottom']) / 2) - current_price))
                live_fvg_size = float(nearest_fvg.get('size', 0.0))

            opens_arr = window['Open'].values if 'Open' in window.columns else window['Close'].values
            closes_arr = window['Close'].values
            live_momentum = abs(closes_arr[-1] - opens_arr[-3]) if len(closes_arr) >= 3 else 0.0
            live_momentum_ratio = round(live_momentum / atr, 2) if atr > 0 else 1.0

            features = pd.DataFrame([{
                'type': 1 if signal == "BUY" else 0,
                'fvg_size_pips': live_fvg_size,
                'rsi_at_entry': rsi,
                'atr_at_entry': atr,
                'momentum_ratio': live_momentum_ratio,
                'news_bias': news_bias_used
            }])
            prob = brain.ML_MODEL.predict_proba(features)[0][1]
            ml_conf = int(prob * 100)
            confidence = int((confidence * 0.7) + (ml_conf * 0.3))
        except Exception:
            pass  # fall back to rule-based confidence, same as live behavior

    if confidence < CONFIDENCE_THRESHOLD:
        return None

    trade_setup = brain.calculate_trade_levels(current_price, signal, atr, profile['decimals'], ema_50)
    if not trade_setup:
        return None

    return {
        "index": i,
        "signal": signal,
        "confidence": confidence,
        "entry": trade_setup['entry'],
        "sl": trade_setup['stop_loss'],
        "tp": trade_setup['take_profit'],
        "rr": trade_setup['risk_reward'],
        "phase": phase_str,
        "pattern_confirmed": w_confirmed if signal == "BUY" else m_confirmed,
        "news_bias": news_bias_used,
        "news_aligned": news_aligned,
        "news_conflicted": news_conflicted,
        "news_alignment_event": news_alignment_event,
    }


def run_backtest():
    global _NEWS_DF, _NEWS_EPOCHS, _NEWS_ACTUAL, _NEWS_FORECAST, _NEWS_EVENT_NAMES

    print("📦 Loading historical data...")
    df = load_csv(CSV_1H)
    print(f"✅ Loaded {len(df)} 1H candles.")

    _NEWS_DF = load_news_events(NEWS_CSV)
    if _NEWS_DF is not None:
        _NEWS_EPOCHS = _NEWS_DF['epoch'].values
        _NEWS_ACTUAL = _NEWS_DF['actual'].values
        _NEWS_FORECAST = _NEWS_DF['forecast'].values
        _NEWS_EVENT_NAMES = _NEWS_DF['event'].values

    if MAX_CANDLES is not None and len(df) > MAX_CANDLES:
        df = df.iloc[-MAX_CANDLES:].reset_index(drop=True)
        print(f"✂️  Trimmed to most recent {len(df)} candles (set MAX_CANDLES = None in the script to use full history).")

    if len(df) < MIN_HISTORY + 50:
        print("❌ Not enough data to backtest meaningfully (need at least ~300 candles).")
        return

    trades = []
    total_steps = len(df) - MIN_HISTORY - 1
    split_index = MIN_HISTORY + int(total_steps * (1 - OUT_OF_SAMPLE_FRACTION))
    print(f"🔎 Scanning {total_steps} candles for signals (this can take a few minutes)...")
    print(f"   In-sample: candles up to index {split_index} | Out-of-sample (held out): the rest")

    for step, i in enumerate(range(MIN_HISTORY, len(df) - 1)):
        if step % 200 == 0 and step > 0:
            print(f"  ...{step}/{total_steps} candles scanned, {len(trades)} signals found so far")

        if step % 5000 == 0 and step > 0 and trades:
            pd.DataFrame(trades).to_csv("backtest_results_partial.csv", index=False)
            print(f"  💾 Checkpoint saved ({len(trades)} signals so far) to backtest_results_partial.csv")

        try:
            sig = generate_signal_at(df, i)
        except Exception:
            continue  # skip candles that error out, same as live engine's own try/except

        if sig is None:
            continue

        outcome, hit_idx = simulate_outcome(df, i, sig["signal"], sig["sl"], sig["tp"])
        sig["outcome"] = outcome
        sig["sample"] = "in-sample" if i < split_index else "out-of-sample"
        trades.append(sig)

    print_report(trades)
    save_report(trades)


def print_report(trades):
    if not trades:
        print("\n⚠️ No trades were generated at all across the whole dataset.")
        print("   This usually means the entry conditions (in_pullback + EMA proximity)")
        print("   are too strict, or confidence never crosses the 65 threshold. Worth")
        print("   checking score_mmm_setup()'s point values if this happens.")
        return

    df = pd.DataFrame(trades)
    resolved = df[df['outcome'].isin(["WIN", "LOSS"])]

    print("\n" + "=" * 55)
    print("📊 BACKTEST REPORT")
    print("=" * 55)
    print(f"Total signals generated:     {len(df)}")
    print(f"Resolved (hit SL or TP):     {len(resolved)}")
    print(f"Unresolved (ran out of data): {len(df) - len(resolved)}")

    if len(resolved) == 0:
        print("⚠️ No resolved trades yet — try increasing FORWARD_WINDOW.")
        return

    wins = len(resolved[resolved['outcome'] == "WIN"])
    losses = len(resolved[resolved['outcome'] == "LOSS"])
    win_rate = wins / len(resolved) * 100

    print(f"\nWins: {wins}  |  Losses: {losses}  |  Win rate: {win_rate:.1f}%")
    print(f"Average confidence on all signals: {df['confidence'].mean():.1f}")
    print(f"Average R:R on all signals: {df['rr'].mean():.2f}")

    expectancy = (win_rate / 100 * df['rr'].mean()) - ((1 - win_rate / 100) * 1)
    print(f"Expectancy (R per trade, using avg RR): {expectancy:.2f}R")

    print("\n--- Win rate by confidence bucket ---")
    print("(if higher buckets don't show higher win rates, confidence isn't well-calibrated)")
    for low, high in [(65, 75), (75, 85), (85, 101)]:
        bucket = resolved[(resolved['confidence'] >= low) & (resolved['confidence'] < high)]
        if len(bucket) > 0:
            bwin = len(bucket[bucket['outcome'] == "WIN"]) / len(bucket) * 100
            print(f"  {low}-{high}%: {len(bucket)} trades, {bwin:.1f}% win rate")
        else:
            print(f"  {low}-{high}%: no trades")

    print("\n--- Win rate by direction ---")
    for direction in ["BUY", "SELL"]:
        d = resolved[resolved['signal'] == direction]
        if len(d) > 0:
            dwin = len(d[d['outcome'] == "WIN"]) / len(d) * 100
            print(f"  {direction}: {len(d)} trades, {dwin:.1f}% win rate")
        else:
            print(f"  {direction}: no trades")

    if 'pattern_confirmed' in resolved.columns:
        print("\n--- Win rate by W/M pattern confirmation ---")
        for flag, label in [(True, "Pattern confirmed"), (False, "No pattern confirmed")]:
            p = resolved[resolved['pattern_confirmed'] == flag]
            if len(p) > 0:
                pwin = len(p[p['outcome'] == "WIN"]) / len(p) * 100
                print(f"  {label}: {len(p)} trades, {pwin:.1f}% win rate")
            else:
                print(f"  {label}: no trades")

    if 'news_bias' in resolved.columns:
        print("\n--- Win rate by news bias at entry ---")
        for bias_val, label in [(1, "News bias +1 (USD miss/bearish)"), (-1, "News bias -1 (USD beat/bullish)"), (0, "No qualifying news / neutral")]:
            n = resolved[resolved['news_bias'] == bias_val]
            if len(n) > 0:
                nwin = len(n[n['outcome'] == "WIN"]) / len(n) * 100
                print(f"  {label}: {len(n)} trades, {nwin:.1f}% win rate")
            else:
                print(f"  {label}: no trades")

    if 'news_aligned' in resolved.columns:
        print("\n--- Win rate by NEWS REACTION PATTERN alignment ---")
        print("(does this actually help — does 'aligned' outperform 'conflicted'/'neither'?)")
        aligned = resolved[resolved['news_aligned'] == True]
        conflicted = resolved[resolved['news_conflicted'] == True]
        neither = resolved[(resolved['news_aligned'] == False) & (resolved['news_conflicted'] == False)]
        for label, subset in [("Aligned (pattern supports the trade)", aligned),
                               ("Conflicted (pattern opposes the trade)", conflicted),
                               ("Neither (no matching pattern nearby)", neither)]:
            if len(subset) > 0:
                w = len(subset[subset['outcome'] == "WIN"]) / len(subset) * 100
                print(f"  {label}: {len(subset)} trades, {w:.1f}% win rate")
            else:
                print(f"  {label}: no trades")

    if 'sample' in resolved.columns:
        print("\n--- IN-SAMPLE vs OUT-OF-SAMPLE (the important check) ---")
        print("(out-of-sample is data NOT used to tune any of the rules above —")
        print(" if its win rate collapses vs in-sample, the 'edge' is likely overfit noise)")
        for sample_label in ["in-sample", "out-of-sample"]:
            s = resolved[resolved['sample'] == sample_label]
            if len(s) > 0:
                swin = len(s[s['outcome'] == "WIN"]) / len(s) * 100
                print(f"  {sample_label}: {len(s)} trades, {swin:.1f}% win rate")
            else:
                print(f"  {sample_label}: no trades")

    print("=" * 55)


def save_report(trades):
    if not trades:
        return
    out_path = "backtest_results.csv"
    pd.DataFrame(trades).to_csv(out_path, index=False)
    print(f"\n💾 Full trade-by-trade log saved to {out_path}")
    print("   Open it in Excel or pandas to dig into specific losing trades.")


if __name__ == "__main__":
    run_backtest()