"""
news_impact_analysis.py

Measures how price actually reacted to historical news events: how many
points/pips it moved after each event, in which direction relative to the
actual-vs-forecast surprise, and how that reaction evolved over the hours
following the release. This is meant to inform strategy design directly —
e.g. whether a dedicated news-reaction entry makes sense, and what
stop/target distances would be realistic — rather than just feeding a
single bias score into an unrelated strategy's confidence calculation.

Reuses the same CSV-parsing logic as backtest.py (load_csv, load_news_events)
so there's one source of truth for how price/news data gets read and cleaned.

Usage:
    cd backend/aura_brain
    python news_impact_analysis.py

Output:
    Prints an overall summary and a per-event-type breakdown to the terminal.
    Saves every individual event's measured reaction to news_impact_results.csv
    for further analysis in Excel/pandas.

IMPORTANT CAVEATS (read before trusting the numbers):
  - Data is 1H candles. This can't measure fast intra-hour reactions — only
    from the next candle boundary onward. True tick/minute data would be
    needed to measure reaction speed precisely.
  - Multiple news events sometimes fire at the exact same time. Price moves
    measured for one can't cleanly isolate its individual effect from the
    others also happening then. This is standard event-study noise, not a
    bug — treat results as directional evidence, not a controlled experiment.
  - News data currently ends 2024-08-16 (see backtest.py's load_news_events
    for why) — this can't tell you anything about reactions after that date.
"""

import numpy as np
import pandas as pd
import backtest  # reuse load_csv() and load_news_events() — single source of truth for parsing

CSV_1H = "1h.csv"
NEWS_CSV = "scrape.csv"

# Gold (XAU/USD) reacts primarily to USD data. Set to None to include all currencies.
CURRENCY_FILTER = "USD"

# How many candles (hours) after each event to measure price at.
FORWARD_OFFSETS = [1, 2, 4, 8, 24, 48]

# Only report per-event-type breakdowns for event names that occurred at
# least this many times — otherwise the average is too noisy to mean anything.
MIN_EVENT_COUNT = 15


def analyze_news_impact():
    print("📦 Loading price history and news events...")
    price_df = backtest.load_csv(CSV_1H)
    print(f"✅ Loaded {len(price_df)} 1H candles.")

    news_df = backtest.load_news_events(NEWS_CSV)
    if news_df is None:
        print("❌ Could not load news events, aborting.")
        return

    if CURRENCY_FILTER:
        before = len(news_df)
        news_df = news_df[news_df['currency'].astype(str).str.upper() == CURRENCY_FILTER].reset_index(drop=True)
        print(f"   Filtered to {len(news_df)} {CURRENCY_FILTER} events (from {before} total high-impact events).")

    price_epochs = price_df['Date'].values
    price_closes = price_df['Close'].values

    records = []
    skipped_no_price_data = 0
    skipped_no_surprise = 0

    for _, row in news_df.iterrows():
        event_epoch = row['epoch']
        actual = row['actual']
        forecast = row['forecast']

        if pd.isna(actual) or pd.isna(forecast) or actual == forecast:
            skipped_no_surprise += 1
            continue

        # index of the first candle AT OR AFTER the event = the "reaction candle"
        idx = np.searchsorted(price_epochs, event_epoch, side='left')
        if idx <= 0 or idx >= len(price_closes):
            skipped_no_price_data += 1
            continue  # event falls outside the price history's covered range

        pre_price = price_closes[idx - 1]  # last known price BEFORE the event
        # +1 = actual missed forecast (commonly framed as USD-bearish / gold-bullish)
        # -1 = actual beat forecast (commonly framed as USD-bullish / gold-bearish)
        surprise_dir = 1 if actual < forecast else -1

        rec = {
            "event": row['event'],
            "datetime": row['datetime'],
            "currency": row['currency'],
            "actual": actual,
            "forecast": forecast,
            "surprise_dir": surprise_dir,
        }
        for offset in FORWARD_OFFSETS:
            future_idx = idx + offset
            if future_idx < len(price_closes):
                move = price_closes[future_idx] - pre_price
                rec[f"move_{offset}h"] = move
                rec[f"correct_dir_{offset}h"] = bool((move > 0 and surprise_dir == 1) or (move < 0 and surprise_dir == -1))
            else:
                rec[f"move_{offset}h"] = None
                rec[f"correct_dir_{offset}h"] = None

        records.append(rec)

    if skipped_no_price_data:
        print(f"   Skipped {skipped_no_price_data} events outside the price history's date range.")
    if skipped_no_surprise:
        print(f"   Skipped {skipped_no_surprise} events with missing/tied actual vs forecast.")

    if not records:
        print("❌ No qualifying events with matching price data found.")
        return

    results_df = pd.DataFrame(records)
    print(f"\n✅ Measured price reaction for {len(results_df)} events.")

    results_df.to_csv("news_impact_results.csv", index=False)
    print("💾 Full per-event results saved to news_impact_results.csv")

    print_summary(results_df)


# Which horizons to show in the main per-event breakdown table.
# (news_impact_results.csv still has every horizon in FORWARD_OFFSETS for deeper digging.)
SUMMARY_HORIZONS = [1, 4, 24]


def print_summary(df):
    print("\n" + "=" * 90)
    print("📊 WHEN THIS NEWS DROPS, DOES GOLD GO UP OR DOWN? (raw historical frequency)")
    print("=" * 90)
    print(f"Only showing event types with at least {MIN_EVENT_COUNT} occurrences.")
    print("Sorted by STATISTICAL CONFIDENCE, not raw %  — a big skew on a tiny sample")
    print("(e.g. 75% on just 16 events) can easily be pure chance, the same way 20 coin")
    print("flips can land 14 heads without the coin being biased. The 'confidence' column")
    print("accounts for sample size so you know which patterns are actually trustworthy.\n")
    print("Confidence guide:  HIGH = very unlikely to be chance (worth building a rule on)")
    print("                   MED  = probably real but not certain — watch, don't bet the farm")
    print("                   LOW  = statistically indistinguishable from a coin flip\n")

    for offset in SUMMARY_HORIZONS:
        move_col = f"move_{offset}h"
        valid = df.dropna(subset=[move_col]).copy()
        if valid.empty:
            continue

        valid['direction'] = np.where(valid[move_col] > 0, 'UP', np.where(valid[move_col] < 0, 'DOWN', 'FLAT'))

        rows = []
        for event, g in valid.groupby('event'):
            n = len(g)
            if n < MIN_EVENT_COUNT:
                continue
            up = g[g['direction'] == 'UP']
            down = g[g['direction'] == 'DOWN']
            pct_up = len(up) / n * 100
            pct_down = len(down) / n * 100
            avg_pips_up = up[move_col].mean() if len(up) > 0 else 0.0
            avg_pips_down = down[move_col].abs().mean() if len(down) > 0 else 0.0

            # z-score: how many standard errors the dominant side is away from a 50/50 coin flip.
            # This is what actually accounts for sample size — the same % means much more with n=200 than n=16.
            p = max(pct_up, pct_down) / 100
            standard_error = np.sqrt(0.25 / n)
            z_score = (p - 0.5) / standard_error

            if z_score >= 2.58:
                confidence = "HIGH"
            elif z_score >= 1.65:
                confidence = "MED"
            else:
                confidence = "LOW"

            rows.append({
                "event": event,
                "n": n,
                "% UP": round(pct_up, 1),
                "avg pips UP": round(avg_pips_up, 2),
                "% DOWN": round(pct_down, 1),
                "avg pips DOWN": round(avg_pips_down, 2),
                "confidence": confidence,
                "z_score": round(z_score, 2),
            })

        if not rows:
            continue

        table = pd.DataFrame(rows).sort_values("z_score", ascending=False).drop(columns="z_score")
        print(f"--- +{offset}h after the news ---")
        print(table.head(25).to_string(index=False))
        print()

    print("=" * 90)
    print("Read it like this: if 'Non-Farm Employment Change' shows 72.0% UP with")
    print("avg pips UP = 9.4 and confidence = HIGH, that means historically, 72% of the")
    print("time gold was HIGHER than before the release by that point (averaging +9.4")
    print("when it was), AND the sample size is large enough that this is very unlikely")
    print("to be a fluke. If confidence = LOW, treat the % as noise even if it looks big.")
    print("=" * 90)
    print("(Every event, every horizon, every individual occurrence is saved in")
    print(" news_impact_results.csv if you want to dig into specific dates.)")


if __name__ == "__main__":
    analyze_news_impact()