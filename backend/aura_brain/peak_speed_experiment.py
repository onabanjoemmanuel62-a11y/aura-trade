"""
peak_speed_experiment.py

Tests whether we can detect W/M reversal patterns FASTER than the current
3-5 day lag, and what that costs in accuracy — using two independent levers:

  1. Swing-detection sensitivity (swing_order): smaller = faster confirmation,
     more false positives, on the SAME 1H data.
  2. Timeframe granularity: detecting the same underlying pattern on 15m/5m/30m
     candles instead of 1H. The lag is measured in CANDLES, not calendar time,
     so the same "confirm after N candles" rule takes 4x less real time on
     15m data than on 1H data.

Reuses brain.py's actual detection logic and backtest.py's actual decision
logic directly (via the swing_order_override hook added to both) — this is
NOT a separate reimplementation, so results are directly comparable to every
other backtest run in this project.

Usage:
    cd backend/aura_brain
    python peak_speed_experiment.py

Output:
    A comparison table: for each configuration, signal count, win rate,
    expectancy, and — the actual point of this experiment — average
    detection lag in BOTH candles and real hours, so you can see the true
    entry-timing cost/benefit of each approach.
"""

import numpy as np
import pandas as pd
import backtest  # reuse load_csv, generate_signal_at, simulate_outcome — single source of truth

# ─────────────────────────────────────────────────────────────────────────────
# EXPERIMENTS TO RUN
# Add/remove/edit freely. `swing_order_override: None` = current adaptive
# behavior (the live default). A number = fixed sensitivity (smaller = faster).
# `candles_per_hour` scales MIN_HISTORY/ROLLING_WINDOW/FORWARD_WINDOW so every
# experiment looks at the same amount of REAL TIME, keeping the comparison fair.
# ─────────────────────────────────────────────────────────────────────────────
EXPERIMENTS = [
    {"name": "1H baseline (current live default)", "csv": "1h.csv", "candles_per_hour": 1.0, "swing_order_override": None},
    {"name": "15M baseline (adaptive, same rule)",   "csv": "15m.csv", "candles_per_hour": 4.0, "swing_order_override": None},
]

# Base values, matching backtest.py's own 1H defaults exactly (candles_per_hour=1.0 case).
BASE_MIN_HISTORY = 250
BASE_FORWARD_WINDOW = 300
BASE_ROLLING_WINDOW = 500
CONFIDENCE_THRESHOLD = 65

# CRITICAL for a fair comparison: match every experiment to the SAME calendar
# window, regardless of how many candles that represents per timeframe.
# Without this, a 1H run spanning 5 years and a 15M run spanning 1.4 years
# (same row count, very different real time) aren't actually comparable —
# this was a real flaw in the first version of this script.
# None = use each file's full available history (no calendar matching).
DATE_RANGE_YEARS = 8  # extended from 3 — the 3-year run only gave 8-10 out-of-sample
                       # trades, too thin to trust. More years = a firmer verdict.

# Out-of-sample check — same idea as backtest.py: the most recent slice of
# the (already date-matched) window is held out and reported separately.
# If out-of-sample collapses vs in-sample, an "improvement" is likely overfit.
OUT_OF_SAMPLE_FRACTION = 0.20

# Safety cap: even after date-matching, a very fine timeframe (5m, 1m) over
# several years can be a LOT of rows. This trims to the most recent N candles
# WITHIN the date-matched window only if it's still too large to run in
# reasonable time. Raise this once you know how long a full run takes.
MAX_CANDLES_PER_EXPERIMENT = 300000  # raised to accommodate 8 years of 15m data (~280k candles)


def run_one_experiment(config):
    name = config["name"]
    csv_file = config["csv"]
    scale = config["candles_per_hour"]
    swing_order_override = config["swing_order_override"]

    print(f"\n{'='*70}\n▶ {name}\n{'='*70}")

    try:
        df = backtest.load_csv(csv_file)
    except SystemExit:
        print(f"  ⚠️ Skipping — {csv_file} not found in this folder.")
        return None

    full_start = pd.to_datetime(df['Date'].min(), unit='s')
    full_end = pd.to_datetime(df['Date'].max(), unit='s')
    print(f"  Full file range: {full_start.date()} to {full_end.date()} ({len(df)} candles)")

    # Match to the same calendar window across ALL experiments, regardless of
    # timeframe — this is what makes the 1H vs 15M comparison actually fair.
    if DATE_RANGE_YEARS is not None:
        cutoff_epoch = df['Date'].max() - int(DATE_RANGE_YEARS * 365.25 * 24 * 3600)
        df = df[df['Date'] >= cutoff_epoch].reset_index(drop=True)
        used_start = pd.to_datetime(df['Date'].min(), unit='s')
        used_end = pd.to_datetime(df['Date'].max(), unit='s')
        print(f"  Matched to last {DATE_RANGE_YEARS} years: {used_start.date()} to {used_end.date()} ({len(df)} candles)")

    if len(df) > MAX_CANDLES_PER_EXPERIMENT:
        df = df.iloc[-MAX_CANDLES_PER_EXPERIMENT:].reset_index(drop=True)
        print(f"  ⚠️ Still too large — trimmed further to most recent {len(df)} candles (raise MAX_CANDLES_PER_EXPERIMENT to avoid this).")

    min_history = int(BASE_MIN_HISTORY * scale)
    forward_window = int(BASE_FORWARD_WINDOW * scale)
    rolling_window = int(BASE_ROLLING_WINDOW * scale)

    if len(df) < min_history + 50:
        print(f"  ⚠️ Skipping — not enough data in the matched window ({len(df)} candles, need at least {min_history + 50}).")
        return None

    split_index = min_history + int((len(df) - min_history - 1) * (1 - OUT_OF_SAMPLE_FRACTION))

    # Temporarily override backtest.py's module-level constants so its existing,
    # already-validated generate_signal_at/simulate_outcome functions run with
    # this experiment's scaled windows. Restored at the end no matter what.
    original = (backtest.MIN_HISTORY, backtest.FORWARD_WINDOW, backtest.ROLLING_WINDOW, backtest.CONFIDENCE_THRESHOLD)
    backtest.MIN_HISTORY = min_history
    backtest.FORWARD_WINDOW = forward_window
    backtest.ROLLING_WINDOW = rolling_window
    backtest.CONFIDENCE_THRESHOLD = CONFIDENCE_THRESHOLD

    trades = []
    try:
        total_steps = len(df) - min_history - 1
        for step, i in enumerate(range(min_history, len(df) - 1)):
            if step % 1000 == 0 and step > 0:
                print(f"    ...{step}/{total_steps} scanned, {len(trades)} signals so far")
            try:
                sig = backtest.generate_signal_at(df, i, swing_order_override=swing_order_override)
            except Exception:
                continue
            if sig is None:
                continue
            outcome, _ = backtest.simulate_outcome(df, i, sig["signal"], sig["sl"], sig["tp"])
            sig["outcome"] = outcome
            sig["sample"] = "in-sample" if i < split_index else "out-of-sample"
            trades.append(sig)
    finally:
        backtest.MIN_HISTORY, backtest.FORWARD_WINDOW, backtest.ROLLING_WINDOW, backtest.CONFIDENCE_THRESHOLD = original

    if not trades:
        print("  No signals generated in this configuration.")
        return {"name": name, "signals": 0, "win_rate": None, "expectancy": None,
                "avg_lag_candles": None, "avg_lag_hours": None,
                "in_sample_wr": None, "oos_wr": None, "oos_n": 0}

    tdf = pd.DataFrame(trades)
    resolved = tdf[tdf['outcome'].isin(["WIN", "LOSS"])]
    win_rate = (resolved['outcome'] == "WIN").mean() * 100 if len(resolved) > 0 else None
    avg_rr = resolved['rr'].mean() if len(resolved) > 0 else 2.0
    expectancy = (win_rate / 100 * avg_rr - (1 - win_rate / 100)) if win_rate is not None else None

    in_sample = resolved[resolved['sample'] == 'in-sample']
    out_sample = resolved[resolved['sample'] == 'out-of-sample']
    in_sample_wr = (in_sample['outcome'] == "WIN").mean() * 100 if len(in_sample) > 0 else None
    oos_wr = (out_sample['outcome'] == "WIN").mean() * 100 if len(out_sample) > 0 else None

    lags = tdf['detection_lag_candles'].dropna()
    avg_lag_candles = lags.mean() if len(lags) > 0 else None
    avg_lag_hours = (avg_lag_candles / scale) if avg_lag_candles is not None else None

    print(f"  Signals: {len(tdf)} | Resolved: {len(resolved)} | Win rate: {win_rate:.1f}%" if win_rate is not None else f"  Signals: {len(tdf)} (none resolved)")
    if in_sample_wr is not None or oos_wr is not None:
        is_str = f"{in_sample_wr:.1f}%" if in_sample_wr is not None else "n/a"
        oos_str = f"{oos_wr:.1f}% (n={len(out_sample)})" if oos_wr is not None else f"n/a (n={len(out_sample)})"
        print(f"  In-sample: {is_str} | Out-of-sample: {oos_str}  <- the important check")
    if avg_lag_hours is not None:
        print(f"  Avg detection lag: {avg_lag_candles:.1f} candles = {avg_lag_hours:.1f} hours ({avg_lag_hours/24:.1f} days)")

    return {
        "name": name,
        "signals": len(tdf),
        "win_rate": win_rate,
        "expectancy": expectancy,
        "avg_lag_candles": avg_lag_candles,
        "avg_lag_hours": avg_lag_hours,
        "in_sample_wr": in_sample_wr,
        "oos_wr": oos_wr,
        "oos_n": len(out_sample),
    }


def main():
    results = []
    for config in EXPERIMENTS:
        result = run_one_experiment(config)
        if result:
            results.append(result)

    if not results:
        print("\nNo experiments produced results.")
        return

    print("\n\n" + "=" * 115)
    print("📊 COMPARISON — speed vs. accuracy tradeoff across configurations (same calendar window)")
    print("=" * 115)
    header = f"{'Configuration':<38} {'Signals':>8} {'Win Rate':>10} {'Out-of-Sample':>16} {'Expectancy':>11} {'Avg Lag (hrs)':>14} {'Days':>6}"
    print(header)
    print("-" * len(header))
    for r in results:
        wr = f"{r['win_rate']:.1f}%" if r['win_rate'] is not None else "n/a"
        oos = f"{r['oos_wr']:.1f}% (n={r['oos_n']})" if r.get('oos_wr') is not None else f"n/a (n={r.get('oos_n', 0)})"
        exp = f"{r['expectancy']:.2f}R" if r['expectancy'] is not None else "n/a"
        lag_h = f"{r['avg_lag_hours']:.1f}" if r['avg_lag_hours'] is not None else "n/a"
        lag_d = f"{r['avg_lag_hours']/24:.1f}" if r['avg_lag_hours'] is not None else "n/a"
        print(f"{r['name']:<38} {r['signals']:>8} {wr:>10} {oos:>16} {exp:>11} {lag_h:>14} {lag_d:>6}")
    print("=" * 115)
    print("Read it like this: if '15M faster (order=8)' shows a much lower avg lag")
    print("(hours/days) than '1H baseline' WITHOUT the win rate collapsing — AND its")
    print("Out-of-Sample number holds up close to its overall Win Rate — that's genuine,")
    print("trustworthy evidence, not a fluke from one lucky slice of history.")


if __name__ == "__main__":
    main()