"""
early_reversal_detection.py

Tests three concrete, legitimate mechanisms that could let us spot a reversal
on the SAME candle it happens, instead of waiting for a multi-bar-confirmed
swing pivot (which is what causes the 3-5 day lag we measured earlier):

  1. Candlestick exhaustion — a long rejection wick against the recent trend
     (price pushed further, then got rejected within the same candle).
  2. Volume spike — unusually high volume on the exhaustion candle.
  3. Session timing — whether the candle falls in the London or New York
     session open window (known clustering point for reversals).

This is an EVENT STUDY, same statistical approach as news_impact_analysis.py:
it measures the real, historical hit rate of each mechanism (alone and
combined) with proper significance testing — it does NOT assume any of them
work, and does NOT chase a target win rate. Whatever the real numbers are,
that's the answer.

"Hit" definition: after a candidate fires, does price move in the reversal
direction by REVERSAL_THRESHOLD_ATR (in ATR units) within LOOKAHEAD_CANDLES,
before it moves that same amount further in the original trend direction?
This is a symmetric race, not a fixed-R:R trade simulation — it isolates
whether the SIGNAL itself has predictive value, before any strategy is built
around it.

Usage:
    cd backend/aura_brain
    python early_reversal_detection.py

Output:
    A comparison table across wick-strictness levels and filter combinations,
    each with a HIGH/MED/LOW statistical confidence label (same z-score method
    as news_impact_analysis.py) so small-sample flukes don't get mistaken for
    real edges.
"""

import numpy as np
import pandas as pd
import brain
import backtest  # reuse load_csv — single source of truth for parsing

CSV_FILE = "15m.csv"

ATR_PERIOD = 14
TREND_LOOKBACK = 20          # candles used to determine local trend direction (EMA slope proxy)
LOOKAHEAD_CANDLES = 20        # how far forward to look for the reversal to play out (20 * 15m = 5 hours)
REVERSAL_THRESHOLD_ATR = 1.0  # how far price must move (in ATR units) to count as a real reversal

WICK_MULTIPLIERS = [1.5, 2.0, 3.0]   # how large the rejection wick must be, relative to the candle's body
VOLUME_SPIKE_MULTIPLIER = 2.0        # candidate's volume vs the trailing 20-candle average volume
VOLUME_LOOKBACK = 20

# Known reversal-clustering windows, in UTC hour-of-day (inclusive start, exclusive end).
LONDON_OPEN_WINDOW = (7, 9)
NY_OPEN_WINDOW = (12, 14)

MAX_CANDLES = 200000  # safety cap; raise once you know runtime on your machine


def load_data():
    df = backtest.load_csv(CSV_FILE)
    if len(df) > MAX_CANDLES:
        df = df.iloc[-MAX_CANDLES:].reset_index(drop=True)
        print(f"Trimmed to most recent {len(df)} candles (raise MAX_CANDLES to use more).")
    else:
        print(f"Using all {len(df)} candles.")
    return df


def compute_candidates(df, wick_multiplier):
    """
    Scans the whole dataframe once and returns a list of candidate dicts —
    one per candle where a rejection-wick exhaustion pattern fired, tagged
    with whether it also had a volume spike and/or fell in a session window.
    """
    highs = df['High'].values
    lows = df['Low'].values
    opens = df['Open'].values
    closes = df['Close'].values
    dates = df['Date'].values
    has_volume = 'volume' in df.columns
    volumes = df['volume'].values if has_volume else None

    ema_trend = df['Close'].ewm(span=TREND_LOOKBACK, adjust=False).mean().values

    candidates = []
    start = max(ATR_PERIOD, TREND_LOOKBACK, VOLUME_LOOKBACK) + 1
    end = len(df) - LOOKAHEAD_CANDLES - 1

    for i in range(start, end):
        window_df = df.iloc[max(0, i - 100):i + 1]
        atr = brain.calculate_atr(window_df, ATR_PERIOD)
        if atr == 0 or np.isnan(atr):
            continue

        body = abs(closes[i] - opens[i])
        upper_wick = highs[i] - max(opens[i], closes[i])
        lower_wick = min(opens[i], closes[i]) - lows[i]

        uptrend = closes[i] > ema_trend[i]
        downtrend = closes[i] < ema_trend[i]

        candidate_type = None
        # Bearish candidate: in an uptrend, a long upper wick + red close = rejection at the top
        if uptrend and body > 0 and upper_wick > wick_multiplier * body and closes[i] < opens[i]:
            candidate_type = "BEARISH"
        # Bullish candidate: in a downtrend, a long lower wick + green close = rejection at the bottom
        elif downtrend and body > 0 and lower_wick > wick_multiplier * body and closes[i] > opens[i]:
            candidate_type = "BULLISH"

        if candidate_type is None:
            continue

        volume_spike = False
        if has_volume and i >= VOLUME_LOOKBACK:
            avg_vol = np.mean(volumes[i - VOLUME_LOOKBACK:i])
            if avg_vol > 0:
                volume_spike = volumes[i] >= VOLUME_SPIKE_MULTIPLIER * avg_vol

        hour = pd.to_datetime(dates[i], unit='s').hour
        in_london_open = LONDON_OPEN_WINDOW[0] <= hour < LONDON_OPEN_WINDOW[1]
        in_ny_open = NY_OPEN_WINDOW[0] <= hour < NY_OPEN_WINDOW[1]
        in_session_open = in_london_open or in_ny_open

        # Race to REVERSAL_THRESHOLD_ATR: does price hit the reversal target
        # before it hits the same distance further in the original direction?
        # CRITICAL: both targets must be measured from the SAME reference point
        # (the close) — measuring one from close and the other from high/low
        # would structurally favor "WIN" regardless of any real signal, since
        # a big wick candle already has its high/low far from its close.
        reversal_target_up = closes[i] + REVERSAL_THRESHOLD_ATR * atr
        reversal_target_down = closes[i] - REVERSAL_THRESHOLD_ATR * atr
        outcome = "NO_RESULT"
        for j in range(i + 1, min(i + 1 + LOOKAHEAD_CANDLES, len(df))):
            # Check the ADVERSE outcome first when a single candle's range could
            # satisfy both conditions at once — matches backtest.py's established
            # convention (checks SL before TP) for handling this same ambiguity.
            # Checking the favorable side first would silently bias every result
            # toward "WIN" whenever both are hit in the same wide-range candle.
            if candidate_type == "BEARISH":
                if highs[j] >= reversal_target_up:  # continued up instead — check first
                    outcome = "LOSS"; break
                if lows[j] <= reversal_target_down:
                    outcome = "WIN"; break
            else:  # BULLISH
                if lows[j] <= reversal_target_down:  # continued down instead — check first
                    outcome = "LOSS"; break
                if highs[j] >= reversal_target_up:
                    outcome = "WIN"; break

        candidates.append({
            "index": i, "type": candidate_type, "outcome": outcome,
            "volume_spike": volume_spike, "in_session_open": in_session_open,
            "wick_multiplier": wick_multiplier,
        })

    return candidates


def build_shuffled_control(df, seed=42):
    """
    Creates a control dataset that destroys genuine temporal/trend structure
    while preserving realistic price continuity between candles (candle i+1
    still opens near where candle i closed) and each candle's own wick/body
    SHAPE — both needed for the wick-pattern detector to mean anything on
    the control. Achieved by shuffling the sequence of candle-to-candle
    percentage returns and each candle's own internal OHLC shape together
    (as one unit), then re-walking them into a continuous synthetic price
    path. A naive row-shuffle of raw OHLC values was tried first and
    produced an unrealistic ~84% "win rate" purely from the huge artificial
    gaps it created between shuffled candles — this is the fix for that.
    """
    rng = np.random.RandomState(seed)
    n = len(df)

    closes = df['Close'].values
    opens = df['Open'].values
    highs = df['High'].values
    lows = df['Low'].values

    returns = np.diff(closes) / closes[:-1]
    shuffled_returns = returns.copy()
    rng.shuffle(shuffled_returns)

    # Each candle's own internal shape, as ratios relative to ITS OWN close —
    # shuffled independently so a candle's shape is decoupled from its
    # original position too, while staying internally realistic.
    shape_idx = rng.permutation(n)
    open_ratio = (opens / closes)[shape_idx]
    high_ratio = (highs / closes)[shape_idx]
    low_ratio = (lows / closes)[shape_idx]

    new_closes = np.empty(n)
    new_closes[0] = closes[0]
    for i in range(1, n):
        new_closes[i] = new_closes[i - 1] * (1 + shuffled_returns[i - 1])

    control = df.copy()
    control['Close'] = new_closes
    control['Open'] = new_closes * open_ratio
    control['High'] = new_closes * np.maximum.reduce([high_ratio, open_ratio, np.ones(n)])
    control['Low'] = new_closes * np.minimum.reduce([low_ratio, open_ratio, np.ones(n)])
    if 'volume' in control.columns:
        control['volume'] = df['volume'].values[shape_idx]
    return control


def confidence_label(win_rate_pct, n):
    if n == 0:
        return "LOW"
    p = win_rate_pct / 100
    se = np.sqrt(0.25 / n)
    z = abs(p - 0.5) / se
    if z >= 2.58:
        return "HIGH"
    elif z >= 1.65:
        return "MED"
    return "LOW"


def summarize(candidates, label):
    resolved = [c for c in candidates if c['outcome'] in ("WIN", "LOSS")]
    n = len(resolved)
    if n == 0:
        print(f"  {label:<45} n=0 (no resolved candidates)")
        return None, 0
    win_rate = sum(1 for c in resolved if c['outcome'] == "WIN") / n * 100
    conf = confidence_label(win_rate, n)
    print(f"  {label:<45} n={n:<5} win rate={win_rate:5.1f}%  confidence={conf}")
    return win_rate, n


def main():
    df = load_data()
    control_df = build_shuffled_control(df)

    print("\n" + "=" * 90)
    print("📊 STEP 1: Does the raw candlestick exhaustion pattern alone predict a reversal?")
    print("=" * 90)
    print("(baseline = no volume or session filter, just the wick pattern, at each strictness level)")
    print("(CONTROL = same exact test run on shuffled/randomized data — the honest noise floor)\n")

    all_results = {}
    all_controls = {}
    for wm in WICK_MULTIPLIERS:
        candidates = compute_candidates(df, wm)
        all_results[wm] = candidates
        summarize(candidates, f"Wick >= {wm}x body (all candidates)")

        control_candidates = compute_candidates(control_df, wm)
        all_controls[wm] = control_candidates
        summarize(control_candidates, f"  CONTROL (shuffled) — wick >= {wm}x")
        print()

    print("\n" + "=" * 90)
    print("📊 STEP 2: Does adding volume spike and/or session timing improve it?")
    print("=" * 90)
    print("(using the middle wick strictness level as the base pattern, real vs its own control)\n")

    base_wm = WICK_MULTIPLIERS[len(WICK_MULTIPLIERS) // 2]
    base_candidates = all_results[base_wm]
    base_control = all_controls[base_wm]

    summarize(base_candidates, f"No filter (wick >= {base_wm}x only)")
    summarize(base_control, f"  CONTROL (shuffled)")
    print()
    summarize([c for c in base_candidates if c['volume_spike']], f"+ Volume spike filter")
    summarize([c for c in base_control if c['volume_spike']], f"  CONTROL (shuffled)")
    print()
    summarize([c for c in base_candidates if c['in_session_open']], f"+ Session-open timing filter")
    summarize([c for c in base_control if c['in_session_open']], f"  CONTROL (shuffled)")
    print()
    summarize([c for c in base_candidates if c['volume_spike'] and c['in_session_open']], f"+ Both filters combined")
    summarize([c for c in base_control if c['volume_spike'] and c['in_session_open']], f"  CONTROL (shuffled)")

    print("\n" + "=" * 90)
    print("Read it like this: only trust a result where the REAL win rate is meaningfully")
    print("higher than its OWN matched CONTROL line directly below it — not just far from")
    print("50%. If real and control move together, that's a methodology artifact, not a")
    print("real signal, no matter how confident the real number looks on its own.")
    print("=" * 90)


if __name__ == "__main__":
    main()