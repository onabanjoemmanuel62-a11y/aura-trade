import os
import math
import logging
from typing import List, Optional, Dict, Set
from contextlib import asynccontextmanager
import pandas as pd
import numpy as np
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.responses import Response
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

CSV_FILENAME = "1h.csv"
NODE_URL = "http://127.0.0.1:10000"
DROP_FORMING_CANDLE = True

BASE_SWING_ORDER = 4
FLIP_BUFFER_ATR = 0.30
FLIP_CONFIRM_BARS = 2
ANCHOR_MIN_MOVE_ATR = 1.5
SWING_PROMINENCE_ATR = 0.80
MIN_ALLOWED_RR = 2.0

# Pip-Based TP Floors (with automatic 2.0R minimum guarantee so TP is NEVER smaller than SL!)
FOREX_TP_PIPS = 50.0
GOLD_TP_PIPS = 150.0
MIN_TP_RR_MULTIPLE = 2.0

# Exact MT4 Mobile TDI Parameters (from your 5-step setup guide)
TDI_RSI_PERIOD = 13
TDI_GREEN_SMOOTH = 2
TDI_RED_SIGNAL = 7
TDI_BASE_PERIOD = 34
TDI_BB_STD = 1.618

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}
CYCLE_STATE: Dict[str, Dict] = {}
POI_STATE: Dict[str, Dict] = {}
ORDER_CACHE: Dict[str, int] = {}
ACTIVE_TRADES: Dict[str, Dict] = {}
CLOSED_SIGNALS: Dict[str, Set[int]] = {}

base_dir = os.path.dirname(os.path.abspath(__file__))


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        num = float(value)
        return default if (math.isnan(num) or math.isinf(num)) else num
    except Exception:
        return default


def load_csv_fallback():
    try:
        file_path = os.path.join(os.path.dirname(base_dir), CSV_FILENAME)
        if not os.path.exists(file_path):
            return None
        try:
            df = pd.read_csv(file_path, sep=';')
            if len(df.columns) < 2:
                df = pd.read_csv(file_path, sep=',')
        except Exception:
            df = pd.read_csv(file_path, sep=',')

        df.columns = [c.lower().strip() for c in df.columns]
        rename_map = {
            'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open',
            'date': 'Date', 'time': 'Date', 'timestamp': 'Date'
        }
        df.rename(columns=rename_map, inplace=True)

        numeric_cols = ['Open', 'High', 'Low', 'Close']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], format='mixed', dayfirst=False, errors='coerce')
            df.dropna(subset=['Date'], inplace=True)
            df['Date'] = (df['Date'] - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)

        df.dropna(subset=numeric_cols, inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df if len(df) > 50 else None
    except Exception as e:
        logger.error(f"CSV load failed: {e}")
        return None


def process_live_candles(candles_data: List[Dict]):
    try:
        df = pd.DataFrame(candles_data)
        rename_map = {
            'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open',
            'time': 'Date', 'timestamp': 'Date'
        }
        df.rename(columns=rename_map, inplace=True)
        for c in ['Open', 'High', 'Low', 'Close']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df.dropna(subset=['Open', 'High', 'Low', 'Close'], inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as e:
        logger.error(f"Live candle processing failed: {e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    MARKET_MEMORY["df"] = load_csv_fallback()
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
    currency: str = "XAUUSD"
    current_price: float = 0.0
    candles: Optional[List[Dict]] = None
    htf_candles: Optional[List[Dict]] = None
    news_data: Optional[Dict] = None


def get_instrument_profile(currency: str, current_price: float) -> Dict:
    cu = currency.upper().replace("/", "").replace("-", "").replace("_", "")
    if cu in ("XAUUSD", "GOLD", "GC=F", "GCF"):
        return {"decimals": 2, "pip_size": 0.1, "target_pips": GOLD_TP_PIPS, "label": "XAU/USD"}
    if cu in ("XAGUSD", "SILVER"):
        return {"decimals": 3, "pip_size": 0.001, "target_pips": 60.0, "label": "XAG/USD"}
    if "JPY" in cu:
        return {"decimals": 3, "pip_size": 0.01, "target_pips": FOREX_TP_PIPS, "label": currency}
    if any(cu.startswith(p) or cu.endswith(p) for p in ("EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "USD")):
        return {"decimals": 5, "pip_size": 0.0001, "target_pips": FOREX_TP_PIPS, "label": currency}
    if current_price > 500:
        return {"decimals": 2, "pip_size": 0.1, "target_pips": GOLD_TP_PIPS, "label": currency}
    if current_price > 20:
        return {"decimals": 3, "pip_size": 0.01, "target_pips": FOREX_TP_PIPS, "label": currency}
    return {"decimals": 5, "pip_size": 0.0001, "target_pips": FOREX_TP_PIPS, "label": currency}


def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    high = df['High'].values
    low = df['Low'].values
    close = df['Close'].values
    if len(close) < 2:
        return 0.0
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]))
    )
    if len(tr) < period:
        return float(np.mean(tr))
    atr_series = pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean()
    return float(atr_series.iloc[-1])


def compute_mt4_rsi(closes: np.ndarray, period: int = 13) -> np.ndarray:
    """
    Exact MT4 `iRSI(NULL, 0, 13, PRICE_CLOSE)` Wilder's calculation:
    - Seeds bar `period` with the simple mean of the first `period` changes,
      then applies Wilder's recursive smoothing `(prev * (period - 1) + curr) / period`.
    """
    n = len(closes)
    rsi = np.full(n, 50.0, dtype=float)
    if n <= period:
        return rsi

    diffs = np.diff(closes)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    if avg_loss == 0.0:
        rsi[period] = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + float(gains[i - 1])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i - 1])) / period
        if avg_loss == 0.0:
            rsi[i] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    rsi[:period] = rsi[period]
    return rsi


def compute_tdi_arrays(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    """
    Computes the 5 TDI lines matching the exact 5-step MT4 Mobile setup guide:
    - Step 1 (Base):   RSI(13, Apply to Close) -> `First Indicator's Data`
    - Step 2 (Green):  SMA(2, Shift 0, Simple) applied to `Previous/First Indicator's Data` (`RSI 13`)
    - Step 3 (Red):    SMA(7, Shift 0, Simple) applied to `Previous Indicator's Data` (`Green Line`!)
    - Step 4 (Blue):   Bollinger Bands(34, Deviation 1.618, Shift 0, ddof=0) applied to `First Indicator's Data` (`RSI 13`)
    - Step 5 (Yellow): SMA(34, Shift 0, Simple) applied to `First Indicator's Data` (`RSI 13`)
    """
    closes = df['Close'].values.astype(float)
    rsi_raw = pd.Series(compute_mt4_rsi(closes, TDI_RSI_PERIOD))

    green = rsi_raw.rolling(window=TDI_GREEN_SMOOTH, min_periods=1).mean()
    red = green.rolling(window=TDI_RED_SIGNAL, min_periods=1).mean()
    yellow = rsi_raw.rolling(window=TDI_BASE_PERIOD, min_periods=1).mean()
    std34 = rsi_raw.rolling(window=TDI_BASE_PERIOD, min_periods=1).std(ddof=0).fillna(5.0)

    upper = (yellow + TDI_BB_STD * std34).clip(0.0, 100.0)
    lower = (yellow - TDI_BB_STD * std34).clip(0.0, 100.0)

    return {
        "rsi": rsi_raw.values,
        "green": green.values,
        "red": red.values,
        "yellow": yellow.values,
        "upper": upper.values,
        "lower": lower.values,
    }


def evaluate_sharkfin_lifecycle(green: np.ndarray, yellow: np.ndarray,
                                upper: np.ndarray, lower: np.ndarray,
                                apex_idx: int, is_bullish: bool,
                                swept_price_pool: bool = False) -> Optional[Dict]:
    """
    TRUE BTMM SHARKFIN LIFECYCLE EVALUATOR (v12.5):
    1. Strict Zero-M / Zero-W Excursion Law (Fixes NZD/USD Oct 30 M-Trap!):
       - Finds the exact bar (`excursion_start`) where Green crossed above `yellow` (for Sell)
         or below `yellow` (for Buy).
       - Counts EVERY local peak (`^`) or local trough (`v`) from `excursion_start` all the way
         until Green crosses back across `yellow` (`confirm_idx`).
       - If there is MORE THAN 1 PEAK (`peaks > 1`, an "M" shape) or MORE THAN 1 TROUGH
         (`troughs > 1`, a "W" shape) in that excursion -> 100% DISQUALIFIED!
    2. Price-Sweep + Sharp V-Fin Synchronization (Fixes XAU/USD Oct 29 Stop-Hunt!):
       - Standard TDI fin reaches the outer 30% of the Blue Band river (`upper - 0.30 * width`).
       - If price also swept a structural Swing High/Low (`swept_price_pool=True`), a razor-sharp
         single V-fin in the upper half of the river (`yellow + 0.48 * width`) that snaps straight
         back across the Yellow Base Line is also caught!
    """
    n = len(green)
    if apex_idx < 4 or apex_idx >= n - 1:
        return None

    if is_bullish:
        band_width = max(float(yellow[apex_idx] - lower[apex_idx]), 4.0)
        river_allowance = 0.52 if swept_price_pool else 0.30
        river_edge = float(lower[apex_idx]) + river_allowance * band_width

        # 1. Must reach the Lower Blue Band or river edge (and be well below Yellow Base Line)
        if green[apex_idx] > river_edge or green[apex_idx] >= yellow[apex_idx] - 3.5:
            return None

        # 2. Must be a sharp single V-bottom (`v`): lower than previous bar and hooking up on next bar
        if not (green[apex_idx] < green[apex_idx - 1] - 0.5 and green[apex_idx + 1] > green[apex_idx] + 1.0):
            return None

        # 3. Walk back to find `excursion_start` (where Green crossed below Yellow Base Line)
        excursion_start = apex_idx
        while excursion_start > 1 and green[excursion_start - 1] < yellow[excursion_start - 1]:
            excursion_start -= 1
            if (apex_idx - excursion_start) > 10:
                return None

        # Reject if it took too long (> 6 bars from Yellow Base Line to apex) or dragged flat outside Lower Band
        if (apex_idx - excursion_start) > 6:
            return None
        outside_bars = sum(1 for i in range(excursion_start, min(n, apex_idx + 2)) if green[i] < lower[i] - 0.3)
        if outside_bars > 2:
            return None

        # 4. ZERO-W TRAP CHECK BEFORE APEX:
        # From `excursion_start` to `apex_idx`, Green must fall monotonically without bouncing up >= 0.8 pts first!
        for i in range(excursion_start + 1, apex_idx):
            if green[i] > green[i - 1] + 0.75:
                return None

        # 5. Meaningful drop from recent 6-bar high into the V-trough
        recent_high = float(np.max(green[max(0, apex_idx - 6) : apex_idx]))
        if (recent_high - float(green[apex_idx])) < 5.0:
            return None

        # 6. Walk forward from `apex_idx + 1` to check Yellow Base Line crossover vs. W-shape invalidation
        for m in range(apex_idx + 1, n):
            # STRICT ZERO-W RULE AFTER APEX:
            # While rising from `apex_idx` to `yellow`, Green must NEVER dip down by >= 0.6 pts!
            # Any dip before crossing the Yellow Base Line creates a second trough ("W" manipulation lag)!
            if green[m] < green[m - 1] - 0.55:
                return None
            if green[m] <= green[apex_idx] + 0.2:
                return None

            # BASE LINE CONFIRMATION RULE:
            # Confirmed ONLY when Green crosses/closes ABOVE the Yellow Market Base Line!
            if green[m] > yellow[m] + 0.15:
                # Verify that the candle AFTER `m` (if historical) didn't immediately whipsaw back below `yellow`
                if m + 1 < n and green[m + 1] < yellow[m + 1] - 1.5 and (m - apex_idx) > 3:
                    return None
                return {
                    "status": "CONFIRMED",
                    "apex_idx": apex_idx,
                    "confirm_idx": m,
                    "tdi_val": round(float(green[apex_idx]), 1),
                }

            if (m - apex_idx) > 6:
                return None

        # If we reached the live right edge of the chart (`n - 1`) within 5 bars of `apex_idx`
        # and Green is rising cleanly toward the Yellow Base Line with zero dips -> WATCHING!
        if (n - 1 - apex_idx) <= 5 and green[n - 1] > green[apex_idx] and green[n - 1] <= yellow[n - 1] + 0.15:
            return {
                "status": "WATCHING",
                "apex_idx": apex_idx,
                "confirm_idx": None,
                "tdi_val": round(float(green[apex_idx]), 1),
            }

        return None

    else:
        # Bearish Trend (`PFH` -> Watch Upper Blue Band ONLY for SELL Stop-Hunts):
        band_width = max(float(upper[apex_idx] - yellow[apex_idx]), 4.0)
        river_allowance = 0.52 if swept_price_pool else 0.30
        river_edge = float(upper[apex_idx]) - river_allowance * band_width

        # 1. Must reach the Upper Blue Band or river edge (and be well above Yellow Base Line)
        if green[apex_idx] < river_edge or green[apex_idx] <= yellow[apex_idx] + 3.5:
            return None

        # 2. Must be a sharp single inverted-V peak (`^`): higher than previous bar and hooking down on next bar
        if not (green[apex_idx] > green[apex_idx - 1] + 0.5 and green[apex_idx + 1] < green[apex_idx] - 1.0):
            return None

        # 3. Walk back to find `excursion_start` (where Green crossed above Yellow Base Line)
        excursion_start = apex_idx
        while excursion_start > 1 and green[excursion_start - 1] > yellow[excursion_start - 1]:
            excursion_start -= 1
            if (apex_idx - excursion_start) > 10:
                return None

        # Reject if it spent > 6 bars above the Yellow Base Line before peaking (kills NZD/USD Oct 30 M-trap!)
        # or dragged outside the Upper Band for > 2 bars
        if (apex_idx - excursion_start) > 6:
            return None
        outside_bars = sum(1 for i in range(excursion_start, min(n, apex_idx + 2)) if green[i] > upper[i] + 0.3)
        if outside_bars > 2:
            return None

        # 4. ZERO-M TRAP CHECK BEFORE APEX (Fixes NZD/USD!):
        # From `excursion_start` to `apex_idx`, Green must rise monotonically toward `apex_idx`
        # without dipping down >= 0.75 pts first! If it dipped and rose again, `apex_idx` is Peak #2 of an "M"!
        for i in range(excursion_start + 1, apex_idx):
            if green[i] < green[i - 1] - 0.75:
                return None

        # 5. Meaningful rise from recent 6-bar low into the inverted-V peak
        recent_low = float(np.min(green[max(0, apex_idx - 6) : apex_idx]))
        if (float(green[apex_idx]) - recent_low) < 5.0:
            return None

        # 6. Walk forward from `apex_idx + 1` to check Yellow Base Line crossover vs. M-shape invalidation
        for m in range(apex_idx + 1, n):
            # STRICT ZERO-M RULE AFTER APEX:
            # While falling from `apex_idx` to `yellow`, Green must NEVER bounce back up by >= 0.55 pts!
            if green[m] > green[m - 1] + 0.55:
                return None
            if green[m] >= green[apex_idx] - 0.2:
                return None

            # BASE LINE CONFIRMATION RULE:
            # Confirmed ONLY when Green crosses/closes BELOW the Yellow Market Base Line!
            if green[m] < yellow[m] - 0.15:
                if m + 1 < n and green[m + 1] > yellow[m + 1] + 1.5 and (m - apex_idx) > 3:
                    return None
                return {
                    "status": "CONFIRMED",
                    "apex_idx": apex_idx,
                    "confirm_idx": m,
                    "tdi_val": round(float(green[apex_idx]), 1),
                }

            if (m - apex_idx) > 6:
                return None

        # If we reached the live right edge (`n - 1`) within 5 bars of `apex_idx`
        # and Green is falling cleanly toward the Yellow Base Line with zero bounces -> WATCHING!
        if (n - 1 - apex_idx) <= 5 and green[n - 1] < green[apex_idx] and green[n - 1] >= yellow[n - 1] - 0.15:
            return {
                "status": "WATCHING",
                "apex_idx": apex_idx,
                "confirm_idx": None,
                "tdi_val": round(float(green[apex_idx]), 1),
            }

        return None


def build_tdi_visual_series(df: pd.DataFrame, tdi: Dict[str, np.ndarray],
                            fin_apex_map: Optional[Dict[int, str]] = None,
                            max_bars: int = 500) -> List[Dict]:
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(dates)
    start_i = max(0, total - max_bars)
    fin_map = fin_apex_map or {}
    out = []
    for i in range(start_i, total):
        out.append({
            "time": int(dates[i]),
            "green": round(float(tdi["green"][i]), 2),
            "red": round(float(tdi["red"][i]), 2),
            "yellow": round(float(tdi["yellow"][i]), 2),
            "upper": round(float(tdi["upper"][i]), 2),
            "lower": round(float(tdi["lower"][i]), 2),
            "fin_type": fin_map.get(i),
        })
    return out


def adaptive_swing_order(df: pd.DataFrame, atr: float) -> int:
    if len(df) < 30 or atr <= 0:
        return BASE_SWING_ORDER
    recent_closes = df['Close'].tail(20).values
    net_move = abs(recent_closes[-1] - recent_closes[0])
    path_length = np.sum(np.abs(np.diff(recent_closes)))
    efficiency = net_move / path_length if path_length > 0 else 0.5
    return int(np.clip(round(6.0 - (efficiency * 3.0)), 3, 6))


def detect_swings_causal(highs: np.ndarray, lows: np.ndarray, swing_order: int, min_prominence: float):
    n = len(highs)
    left_w = swing_order * 2
    right_w = swing_order
    filtered_highs = []
    filtered_lows = []

    for idx in range(left_w, n - right_w):
        l_highs = highs[idx - left_w : idx]
        r_highs = highs[idx + 1 : idx + right_w + 1]
        if highs[idx] >= np.max(l_highs) and highs[idx] > np.max(r_highs):
            left_prom = highs[idx] - np.min(lows[idx - left_w : idx])
            right_prom = highs[idx] - np.min(lows[idx + 1 : idx + right_w + 1])
            if max(left_prom, right_prom) >= min_prominence and min(left_prom, right_prom) >= min_prominence * 0.45:
                if not filtered_highs or (idx - filtered_highs[-1]) >= right_w:
                    filtered_highs.append(idx)
                elif highs[idx] > highs[filtered_highs[-1]]:
                    filtered_highs[-1] = idx

        l_lows = lows[idx - left_w : idx]
        r_lows = lows[idx + 1 : idx + right_w + 1]
        if lows[idx] <= np.min(l_lows) and lows[idx] < np.min(r_lows):
            left_prom = np.max(highs[idx - left_w : idx]) - lows[idx]
            right_prom = np.max(highs[idx + 1 : idx + right_w + 1]) - lows[idx]
            if max(left_prom, right_prom) >= min_prominence and min(left_prom, right_prom) >= min_prominence * 0.45:
                if not filtered_lows or (idx - filtered_lows[-1]) >= right_w:
                    filtered_lows.append(idx)
                elif lows[idx] < lows[filtered_lows[-1]]:
                    filtered_lows[-1] = idx

    return np.array(filtered_highs, dtype=int), np.array(filtered_lows, dtype=int)


def find_choch_and_bos_from_peak(df: pd.DataFrame, is_bullish: bool, anchor_idx: int,
                                 raw_highs: np.ndarray, raw_lows: np.ndarray,
                                 atr: float, decimals: int) -> List[Dict]:
    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(closes)

    anchor_idx = int(np.clip(anchor_idx, 0, total - 1))
    buf = 0.05 * atr
    events = []

    if is_bullish:
        peak_low_val = float(lows[anchor_idx])
        tight_left = max(0, anchor_idx - 25)
        choch_swing_idx = None

        pre_raw_highs = [
            int(i) for i in raw_highs
            if tight_left <= int(i) < anchor_idx - 1
            and (highs[int(i)] - peak_low_val) >= (1.10 * atr)
        ]
        if pre_raw_highs:
            choch_swing_idx = max(pre_raw_highs)
        else:
            for j in range(anchor_idx - 2, tight_left - 1, -1):
                l_w = max(0, j - 3)
                r_w = min(anchor_idx, j + 3)
                if highs[j] >= np.max(highs[l_w:r_w]) and (highs[j] - peak_low_val) >= (1.10 * atr):
                    choch_swing_idx = j
                    break

        if choch_swing_idx is None:
            wider_left = max(0, anchor_idx - 38)
            win_end = max(wider_left + 1, anchor_idx - 1)
            choch_swing_idx = wider_left + int(np.argmax(highs[wider_left:win_end]))

        nb_lo = max(0, choch_swing_idx - 2)
        nb_hi = min(anchor_idx, choch_swing_idx + 3)
        if nb_hi > nb_lo:
            choch_swing_idx = nb_lo + int(np.argmax(highs[nb_lo:nb_hi]))

        choch_lvl = float(highs[choch_swing_idx])
        choch_break_idx = None
        for m in range(anchor_idx + 1, total):
            if closes[m] > choch_lvl + buf:
                choch_break_idx = m
                events.append({
                    "kind": "CHoCH",
                    "level": round(choch_lvl, decimals),
                    "swing_idx": int(choch_swing_idx),
                    "leg_origin": int(anchor_idx),
                    "start_time": int(dates[choch_swing_idx]),
                    "break_idx": int(m),
                    "end_time": int(dates[m]),
                })
                break

        if choch_break_idx is not None:
            leg_start = choch_break_idx
            last_broken_high = choch_lvl

            while leg_start < total - 3:
                run_high_idx = leg_start
                run_high_val = float(highs[leg_start])
                pullback_low = float(lows[leg_start])
                pullback_bars = 0
                pullback_ready = False
                found_bos = False

                for m in range(leg_start + 1, total):
                    if not pullback_ready:
                        if highs[m] >= run_high_val:
                            run_high_val = float(highs[m])
                            run_high_idx = m
                            pullback_low = float(lows[m])
                            pullback_bars = 0
                        else:
                            pullback_low = min(pullback_low, float(lows[m]))
                            pullback_bars += 1
                            if (
                                (run_high_val - pullback_low) >= (0.40 * atr)
                                and pullback_bars >= 2
                                and run_high_val > last_broken_high + (0.15 * atr)
                            ):
                                pullback_ready = True
                    else:
                        if closes[m] > run_high_val + buf and (m - run_high_idx) >= 2:
                            pb_origin_idx = run_high_idx + int(np.argmin(lows[run_high_idx : m + 1]))
                            events.append({
                                "kind": "BOS",
                                "level": round(run_high_val, decimals),
                                "swing_idx": int(run_high_idx),
                                "leg_origin": int(pb_origin_idx),
                                "start_time": int(dates[run_high_idx]),
                                "break_idx": int(m),
                                "end_time": int(dates[m]),
                            })
                            last_broken_high = run_high_val
                            leg_start = m
                            found_bos = True
                            break

                if not found_bos:
                    break

    else:
        peak_high_val = float(highs[anchor_idx])
        tight_left = max(0, anchor_idx - 25)
        choch_swing_idx = None

        pre_raw_lows = [
            int(i) for i in raw_lows
            if tight_left <= int(i) < anchor_idx - 1
            and (peak_high_val - lows[int(i)]) >= (1.10 * atr)
        ]
        if pre_raw_lows:
            choch_swing_idx = max(pre_raw_lows)
        else:
            for j in range(anchor_idx - 2, tight_left - 1, -1):
                l_w = max(0, j - 3)
                r_w = min(anchor_idx, j + 3)
                if lows[j] <= np.min(lows[l_w:r_w]) and (peak_high_val - lows[j]) >= (1.10 * atr):
                    choch_swing_idx = j
                    break

        if choch_swing_idx is None:
            wider_left = max(0, anchor_idx - 38)
            win_end = max(wider_left + 1, anchor_idx - 1)
            choch_swing_idx = wider_left + int(np.argmin(lows[wider_left:win_end]))

        nb_lo = max(0, choch_swing_idx - 2)
        nb_hi = min(anchor_idx, choch_swing_idx + 3)
        if nb_hi > nb_lo:
            choch_swing_idx = nb_lo + int(np.argmin(lows[nb_lo:nb_hi]))

        choch_lvl = float(lows[choch_swing_idx])
        choch_break_idx = None
        for m in range(anchor_idx + 1, total):
            if closes[m] < choch_lvl - buf:
                choch_break_idx = m
                events.append({
                    "kind": "CHoCH",
                    "level": round(choch_lvl, decimals),
                    "swing_idx": int(choch_swing_idx),
                    "leg_origin": int(anchor_idx),
                    "start_time": int(dates[choch_swing_idx]),
                    "break_idx": int(m),
                    "end_time": int(dates[m]),
                })
                break

        if choch_break_idx is not None:
            leg_start = choch_break_idx
            last_broken_low = choch_lvl

            while leg_start < total - 3:
                run_low_idx = leg_start
                run_low_val = float(lows[leg_start])
                pullback_high = float(highs[leg_start])
                pullback_bars = 0
                pullback_ready = False
                found_bos = False

                for m in range(leg_start + 1, total):
                    if not pullback_ready:
                        if lows[m] <= run_low_val:
                            run_low_val = float(lows[m])
                            run_low_idx = m
                            pullback_high = float(highs[m])
                            pullback_bars = 0
                        else:
                            pullback_high = max(pullback_high, float(highs[m]))
                            pullback_bars += 1
                            if (
                                (pullback_high - run_low_val) >= (0.40 * atr)
                                and pullback_bars >= 2
                                and run_low_val < last_broken_low - (0.15 * atr)
                            ):
                                pullback_ready = True
                    else:
                        if closes[m] < run_low_val - buf and (m - run_low_idx) >= 2:
                            pb_origin_idx = run_low_idx + int(np.argmax(highs[run_low_idx : m + 1]))
                            events.append({
                                "kind": "BOS",
                                "level": round(run_low_val, decimals),
                                "swing_idx": int(run_low_idx),
                                "leg_origin": int(pb_origin_idx),
                                "start_time": int(dates[run_low_idx]),
                                "break_idx": int(m),
                                "end_time": int(dates[m]),
                            })
                            last_broken_low = run_low_val
                            leg_start = m
                            found_bos = True
                            break

                if not found_bos:
                    break

    return events


def detect_tdi_sharkfin_stophunt(df: pd.DataFrame, tdi: Dict[str, np.ndarray],
                                 is_bullish: bool, anchor_idx: int,
                                 struct_events: List[Dict], current_price: float,
                                 atr: float, decimals: int, pip_size: float) -> Dict:
    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(closes)

    green = tdi["green"]
    red = tdi["red"]
    yellow = tdi["yellow"]
    upper = tdi["upper"]
    lower = tdi["lower"]

    live_tdi = {
        "green": round(float(green[-1]), 1) if total > 0 else 50.0,
        "red": round(float(red[-1]), 1) if total > 0 else 50.0,
        "yellow": round(float(yellow[-1]), 1) if total > 0 else 50.0,
        "upper": round(float(upper[-1]), 1) if total > 0 else 68.0,
        "lower": round(float(lower[-1]), 1) if total > 0 else 32.0,
    }

    empty_res = {
        "has_pool": False,
        "swept": False,
        "watching_fin": None,
        "pool_type": "TDI Sharkfin Zone",
        "pool_level": None,
        "pool_idx": None,
        "pool_time": None,
        "pool_line": None,
        "recent_sweeps": [],
        "fin_apex_map": {},
        "live_tdi": live_tdi,
    }
    if total < 25 or anchor_idx >= total - 2:
        return empty_res

    anchor_price = float(lows[anchor_idx]) if is_bullish else float(highs[anchor_idx])
    start_scan = max(anchor_idx + 2, total - 220)

    # 1. Identify structural pullback swing lows (for Bullish PFL) or swing highs (for Bearish PFH)
    pools = []
    if is_bullish:
        for j in range(max(anchor_idx + 2, total - 220), total - 2):
            if (
                lows[j] <= lows[j - 1] and lows[j] <= lows[j - 2]
                and lows[j] <= lows[j + 1] and lows[j] <= lows[j + 2]
            ):
                lvl = float(lows[j])
                if lvl <= anchor_price + (0.02 * atr):
                    continue
                bounce = float(np.max(highs[j + 1 : min(total, j + 10)])) - lvl
                if bounce >= 0.40 * atr:
                    pools.append({"idx": j, "level": lvl, "time": int(dates[j])})
    else:
        for j in range(max(anchor_idx + 2, total - 220), total - 2):
            if (
                highs[j] >= highs[j - 1] and highs[j] >= highs[j - 2]
                and highs[j] >= highs[j + 1] and highs[j] >= highs[j + 2]
            ):
                lvl = float(highs[j])
                if lvl >= anchor_price - (0.02 * atr):
                    continue
                drop = lvl - float(np.min(lows[j + 1 : min(total, j + 10)]))
                if drop >= 0.40 * atr:
                    pools.append({"idx": j, "level": lvl, "time": int(dates[j])})

    # 2. Scan for Clean V-Shaped TDI Sharkfins in the direction of the Peak
    recent_sweeps = []
    watching_fin = None
    fin_apex_map: Dict[int, str] = {}

    for apex_idx in range(max(start_scan, 4), total - 1):
        w_start_pre = max(anchor_idx + 1, apex_idx - 2)
        w_end_pre = min(total, apex_idx + 2)

        if is_bullish:
            ext_pre = float(np.min(lows[w_start_pre:w_end_pre]))
            swept_price_pool = any(
                p["idx"] < apex_idx - 1 and ext_pre <= p["level"] + (0.12 * atr)
                for p in pools
            )
        else:
            ext_pre = float(np.max(highs[w_start_pre:w_end_pre]))
            swept_price_pool = any(
                p["idx"] < apex_idx - 1 and ext_pre >= p["level"] - (0.12 * atr)
                for p in pools
            )

        res = evaluate_sharkfin_lifecycle(
            green, yellow, upper, lower, apex_idx, is_bullish, swept_price_pool=swept_price_pool
        )
        if res is None:
            continue

        status = res["status"]
        confirm_idx = res["confirm_idx"]
        tdi_val = res["tdi_val"]

        if is_bullish:
            w_start = max(anchor_idx + 1, apex_idx - 2)
            w_end = (confirm_idx + 1) if confirm_idx is not None else total
            sweep_low_idx = w_start + int(np.argmin(lows[w_start:w_end]))
            sweep_extreme = float(lows[sweep_low_idx])
            if sweep_extreme <= anchor_price:
                continue

            fin_apex_map[apex_idx] = "BULL"

            if status == "WATCHING":
                watching_fin = {
                    "apex_idx": apex_idx,
                    "apex_time": int(dates[apex_idx]),
                    "sweep_extreme": round(sweep_extreme, decimals),
                    "tdi_val": tdi_val,
                    "yellow_lvl": live_tdi["yellow"],
                    "lower_lvl": live_tdi["lower"],
                }
                continue

            k = int(confirm_idx)
            swept_pool = None
            for p in reversed(pools):
                if p["idx"] < apex_idx and sweep_extreme <= p["level"] + (0.15 * atr):
                    swept_pool = p
                    break

            pool_lvl = round(swept_pool["level"] if swept_pool else sweep_extreme, decimals)
            pool_tm = int(swept_pool["time"] if swept_pool else dates[sweep_low_idx])
            pool_idx_val = int(swept_pool["idx"] if swept_pool else sweep_low_idx)

            recent_sweeps.append({
                "signal_idx": k,
                "signal_time": int(dates[k]),
                "apex_idx": apex_idx,
                "pool_idx": pool_idx_val,
                "pool_time": pool_tm,
                "pool_level": pool_lvl,
                "pool_type": f"TDI Sharkfin ({tdi_val})",
                "sweep_extreme": round(sweep_extreme, decimals),
                "entry_close": round(float(closes[k]), decimals),
                "tdi_val": tdi_val,
                "pattern": f"Confirmed TDI Sharkfin Buy (Closed > Base @ {round(float(yellow[k]), 1)})",
            })

        else:
            w_start = max(anchor_idx + 1, apex_idx - 2)
            w_end = (confirm_idx + 1) if confirm_idx is not None else total
            sweep_high_idx = w_start + int(np.argmax(highs[w_start:w_end]))
            sweep_extreme = float(highs[sweep_high_idx])
            if sweep_extreme >= anchor_price:
                continue

            fin_apex_map[apex_idx] = "BEAR"

            if status == "WATCHING":
                watching_fin = {
                    "apex_idx": apex_idx,
                    "apex_time": int(dates[apex_idx]),
                    "sweep_extreme": round(sweep_extreme, decimals),
                    "tdi_val": tdi_val,
                    "yellow_lvl": live_tdi["yellow"],
                    "upper_lvl": live_tdi["upper"],
                }
                continue

            k = int(confirm_idx)
            swept_pool = None
            for p in reversed(pools):
                if p["idx"] < apex_idx and sweep_extreme >= p["level"] - (0.15 * atr):
                    swept_pool = p
                    break

            pool_lvl = round(swept_pool["level"] if swept_pool else sweep_extreme, decimals)
            pool_tm = int(swept_pool["time"] if swept_pool else dates[sweep_high_idx])
            pool_idx_val = int(swept_pool["idx"] if swept_pool else sweep_high_idx)

            recent_sweeps.append({
                "signal_idx": k,
                "signal_time": int(dates[k]),
                "apex_idx": apex_idx,
                "pool_idx": pool_idx_val,
                "pool_time": pool_tm,
                "pool_level": pool_lvl,
                "pool_type": f"TDI Sharkfin ({tdi_val})",
                "sweep_extreme": round(sweep_extreme, decimals),
                "entry_close": round(float(closes[k]), decimals),
                "tdi_val": tdi_val,
                "pattern": f"Confirmed TDI Sharkfin Sell (Closed < Base @ {round(float(yellow[k]), 1)})",
            })

    recent_sweeps.sort(key=lambda x: x["signal_idx"], reverse=True)

    if watching_fin is not None:
        lvl_r = watching_fin["sweep_extreme"]
        return {
            "has_pool": True,
            "swept": False,
            "watching_fin": watching_fin,
            "pool_type": f"Potential Sharkfin ({watching_fin['tdi_val']})",
            "pool_level": lvl_r,
            "pool_idx": watching_fin["apex_idx"],
            "pool_time": watching_fin["apex_time"],
            "sweep_extreme": lvl_r,
            "pool_line": {
                "level": lvl_r,
                "start_time": watching_fin["apex_time"],
                "end_time": int(dates[-1]),
                "type": f"👀 Potential Sharkfin — Await Base Line ({lvl_r:.{decimals}f})",
                "color": "rgba(255, 214, 0, 0.95)",
                "is_anchor": False,
                "is_struct": True,
                "is_choch": False,
            },
            "recent_sweeps": recent_sweeps,
            "fin_apex_map": fin_apex_map,
            "live_tdi": live_tdi,
        }

    for sw in recent_sweeps:
        if (total - 1 - sw["signal_idx"]) <= 35:
            post_low = float(np.min(lows[sw["signal_idx"] + 1:])) if sw["signal_idx"] + 1 < total else sw["sweep_extreme"]
            post_high = float(np.max(highs[sw["signal_idx"] + 1:])) if sw["signal_idx"] + 1 < total else sw["sweep_extreme"]
            if is_bullish and post_low >= sw["sweep_extreme"] - (0.05 * atr):
                lvl_r = sw["pool_level"]
                return {
                    "has_pool": True,
                    "swept": True,
                    "watching_fin": None,
                    "pool_type": sw["pool_type"],
                    "pool_level": lvl_r,
                    "pool_idx": sw["pool_idx"],
                    "pool_time": sw["pool_time"],
                    "sweep_extreme": sw["sweep_extreme"],
                    "pool_line": {
                        "level": lvl_r,
                        "start_time": sw["pool_time"],
                        "end_time": int(dates[-1]),
                        "type": f"🦈 {sw['pool_type']} CONFIRMED ✅ ({lvl_r:.{decimals}f})",
                        "color": "rgba(192, 132, 252, 0.95)",
                        "is_anchor": False,
                        "is_struct": True,
                        "is_choch": False,
                    },
                    "recent_sweeps": recent_sweeps,
                    "fin_apex_map": fin_apex_map,
                    "live_tdi": live_tdi,
                }
            elif (not is_bullish) and post_high <= sw["sweep_extreme"] + (0.05 * atr):
                lvl_r = sw["pool_level"]
                return {
                    "has_pool": True,
                    "swept": True,
                    "watching_fin": None,
                    "pool_type": sw["pool_type"],
                    "pool_level": lvl_r,
                    "pool_idx": sw["pool_idx"],
                    "pool_time": sw["pool_time"],
                    "sweep_extreme": sw["sweep_extreme"],
                    "pool_line": {
                        "level": lvl_r,
                        "start_time": sw["pool_time"],
                        "end_time": int(dates[-1]),
                        "type": f"🦈 {sw['pool_type']} CONFIRMED ✅ ({lvl_r:.{decimals}f})",
                        "color": "rgba(192, 132, 252, 0.95)",
                        "is_anchor": False,
                        "is_struct": True,
                        "is_choch": False,
                    },
                    "recent_sweeps": recent_sweeps,
                    "fin_apex_map": fin_apex_map,
                    "live_tdi": live_tdi,
                }

    unswept_pools = []
    for p in pools:
        p_idx, p_lvl, p_time = p["idx"], p["level"], p["time"]
        breached = False
        for k in range(p_idx + 2, total):
            if is_bullish and lows[k] < p_lvl - (0.05 * atr):
                breached = True
                break
            elif (not is_bullish) and highs[k] > p_lvl + (0.05 * atr):
                breached = True
                break
        if not breached:
            unswept_pools.append(p)

    if unswept_pools:
        chosen = unswept_pools[-1]
        lvl_r = round(chosen["level"], decimals)
        p_label = "Swing Low Bait" if is_bullish else "Swing High Bait"
        return {
            "has_pool": True,
            "swept": False,
            "watching_fin": None,
            "pool_type": p_label,
            "pool_level": lvl_r,
            "pool_idx": chosen["idx"],
            "pool_time": chosen["time"],
            "sweep_extreme": lvl_r,
            "pool_line": {
                "level": lvl_r,
                "start_time": chosen["time"],
                "end_time": int(dates[-1]),
                "type": f"$$$ {p_label} ({lvl_r:.{decimals}f})",
                "color": "rgba(192, 132, 252, 0.92)",
                "is_anchor": False,
                "is_struct": True,
                "is_choch": False,
            },
            "recent_sweeps": recent_sweeps,
            "fin_apex_map": fin_apex_map,
            "live_tdi": live_tdi,
        }

    empty_res["fin_apex_map"] = fin_apex_map
    return empty_res


def compute_pip_based_target(df: pd.DataFrame, is_bullish: bool, ref_entry: float,
                             sl_risk_dist: float, anchor_idx: int,
                             profile: Dict) -> List[Dict]:
    """
    PIP-BASED TAKE PROFIT ENGINE (v12.5):
    - Sets TP using a clean Pip Target (`50 pips` for Forex Majors, `150 pips` / `$15.00` for Gold).
    - Guarantees `TP_distance >= 2.0 * SL_distance` so TP is NEVER smaller than SL!
    """
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(dates)
    if total == 0:
        return []

    decimals = profile.get("decimals", 5)
    pip_size = profile.get("pip_size", 0.0001)
    base_tp_pips = profile.get("target_pips", FOREX_TP_PIPS)

    min_pip_dist = base_tp_pips * pip_size
    rr_floor_dist = sl_risk_dist * MIN_TP_RR_MULTIPLE
    final_tp_dist = max(min_pip_dist, rr_floor_dist)
    final_pips = round(final_tp_dist / max(pip_size, 1e-9), 1)

    end_time = int(dates[-1])
    anchor_idx = int(np.clip(anchor_idx, 0, total - 1))
    start_time = int(dates[max(anchor_idx, total - 35)])

    if is_bullish:
        tp_lvl = round(ref_entry + final_tp_dist, decimals)
    else:
        tp_lvl = round(ref_entry - final_tp_dist, decimals)

    return [{
        "target_num": 1,
        "level": tp_lvl,
        "pips": final_pips,
        "start_time": start_time,
        "end_time": end_time,
        "label": f"Target +{final_pips}p ({tp_lvl:.{decimals}f})",
    }]


def analyze_market_structure(df: pd.DataFrame, profile: Dict,
                             current_price: float = 0.0,
                             swing_order_override: Optional[int] = None,
                             state_key: Optional[str] = None,
                             drop_forming: bool = False) -> Dict:
    highs = df['High'].values.copy()
    lows = df['Low'].values.copy()
    closes = df['Close'].values.copy()
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(closes)
    decimals = profile.get('decimals', 5)
    pip_size = profile.get('pip_size', 0.0001)

    atr = calculate_atr(df, 14)
    if atr == 0:
        atr = float(df['Close'].mean()) * 0.001

    tdi_arrays = compute_tdi_arrays(df)

    fresh_order = adaptive_swing_order(df, atr)
    if swing_order_override is not None:
        swing_order = swing_order_override
    elif state_key:
        cached = ORDER_CACHE.get(state_key)
        if cached is None or abs(fresh_order - cached) >= 3:
            ORDER_CACHE[state_key] = fresh_order
            cached = fresh_order
        swing_order = cached
    else:
        swing_order = fresh_order

    min_prominence = atr * SWING_PROMINENCE_ATR
    raw_highs, raw_lows = detect_swings_causal(highs, lows, swing_order, min_prominence)

    ema_50_array = df['Close'].ewm(span=50, adjust=False).mean().values
    ema_200_array = df['Close'].ewm(span=200, adjust=False).mean().values

    def compute_structural_cycles(seed_bullish: bool):
        highs_set = set(raw_highs.tolist())
        lows_set = set(raw_lows.tolist())

        is_bull = seed_bullish
        cycle_start_idx = 0
        extreme_price = highs[0] if is_bull else lows[0]

        protected_low_idx = 0
        protected_high_idx = 0
        last_confirmed_low = None
        last_confirmed_high = None

        buf = FLIP_BUFFER_ATR * atr
        pending = 0
        events = []

        for i in range(total):
            confirmed_bar = i - swing_order
            if confirmed_bar in highs_set:
                last_confirmed_high = confirmed_bar
            if confirmed_bar in lows_set:
                last_confirmed_low = confirmed_bar

            if is_bull:
                if highs[i] >= extreme_price:
                    extreme_price = highs[i]
                    if last_confirmed_low is not None and last_confirmed_low >= cycle_start_idx:
                        protected_low_idx = last_confirmed_low

                if protected_low_idx == cycle_start_idx and last_confirmed_low is not None and last_confirmed_low > cycle_start_idx:
                    if highs[i] - lows[last_confirmed_low] >= ANCHOR_MIN_MOVE_ATR * atr:
                        protected_low_idx = last_confirmed_low

                struct_broke = closes[i] < (lows[protected_low_idx] - buf)
                pending = pending + 1 if struct_broke else 0

                if pending >= FLIP_CONFIRM_BARS:
                    true_peak_high_idx = cycle_start_idx + int(np.argmax(highs[cycle_start_idx : i + 1]))
                    is_bull = False
                    cycle_start_idx = true_peak_high_idx
                    extreme_price = lows[i]
                    protected_high_idx = true_peak_high_idx
                    pending = 0
                    events.append({
                        "flip_idx": i,
                        "flip_time": int(dates[i]),
                        "bullish": False,
                        "anchor_idx": true_peak_high_idx,
                        "anchor_time": int(dates[true_peak_high_idx]),
                        "anchor_price": float(highs[true_peak_high_idx]),
                    })
            else:
                if lows[i] <= extreme_price:
                    extreme_price = lows[i]
                    if last_confirmed_high is not None and last_confirmed_high >= cycle_start_idx:
                        protected_high_idx = last_confirmed_high

                if protected_high_idx == cycle_start_idx and last_confirmed_high is not None and last_confirmed_high > cycle_start_idx:
                    if highs[last_confirmed_high] - lows[i] >= ANCHOR_MIN_MOVE_ATR * atr:
                        protected_high_idx = last_confirmed_high

                struct_broke = closes[i] > (highs[protected_high_idx] + buf)
                pending = pending + 1 if struct_broke else 0

                if pending >= FLIP_CONFIRM_BARS:
                    true_peak_low_idx = cycle_start_idx + int(np.argmin(lows[cycle_start_idx : i + 1]))
                    is_bull = True
                    cycle_start_idx = true_peak_low_idx
                    extreme_price = highs[i]
                    protected_low_idx = true_peak_low_idx
                    pending = 0
                    events.append({
                        "flip_idx": i,
                        "flip_time": int(dates[i]),
                        "bullish": True,
                        "anchor_idx": true_peak_low_idx,
                        "anchor_time": int(dates[true_peak_low_idx]),
                        "anchor_price": float(lows[true_peak_low_idx]),
                    })

        return events, is_bull

    seed_idx = min(50, total - 1)
    seed_bullish = bool(ema_50_array[seed_idx] > ema_200_array[seed_idx])
    cycle_events, is_bullish = compute_structural_cycles(seed_bullish)

    matching_events = [e for e in cycle_events if e["bullish"] == is_bullish]
    if matching_events:
        latest_event = matching_events[-1]
        fresh_anchor_idx = latest_event["anchor_idx"]
        fresh_anchor_time = latest_event["anchor_time"]
        fresh_anchor_price = latest_event["anchor_price"]
    else:
        fresh_anchor_idx = int(np.argmin(lows)) if is_bullish else int(np.argmax(highs))
        fresh_anchor_time = int(dates[fresh_anchor_idx])
        fresh_anchor_price = float(lows[fresh_anchor_idx]) if is_bullish else float(highs[fresh_anchor_idx])

    macro_lookback = max(0, total - 260)
    macro_max_idx = macro_lookback + int(np.argmax(highs[macro_lookback:]))
    macro_min_idx = macro_lookback + int(np.argmin(lows[macro_lookback:]))
    macro_high_val = float(highs[macro_max_idx])
    macro_low_val = float(lows[macro_min_idx])
    macro_span = macro_high_val - macro_low_val

    if macro_span > (3.5 * atr):
        if macro_max_idx < macro_min_idx:
            inter_highs = [
                int(i) for i in raw_highs
                if macro_max_idx + 3 <= int(i) <= macro_min_idx - 2
                and (highs[int(i)] - macro_low_val) >= (1.5 * atr)
            ]
            reversal_ceiling = float(highs[inter_highs[-1]]) if inter_highs else (macro_low_val + 0.38 * macro_span)
            post_low_max_close = float(np.max(closes[macro_min_idx:]))
            if post_low_max_close <= reversal_ceiling + (0.10 * atr):
                is_bullish = False
                fresh_anchor_idx = int(macro_max_idx)
                fresh_anchor_time = int(dates[macro_max_idx])
                fresh_anchor_price = macro_high_val
            else:
                is_bullish = True
                fresh_anchor_idx = int(macro_min_idx)
                fresh_anchor_time = int(dates[macro_min_idx])
                fresh_anchor_price = macro_low_val
        else:
            inter_lows = [
                int(i) for i in raw_lows
                if macro_min_idx + 3 <= int(i) <= macro_max_idx - 2
                and (macro_high_val - lows[int(i)]) >= (1.5 * atr)
            ]
            reversal_floor = float(lows[inter_lows[-1]]) if inter_lows else (macro_high_val - 0.38 * macro_span)
            post_high_min_close = float(np.min(closes[macro_max_idx:]))
            if post_high_min_close >= reversal_floor - (0.10 * atr):
                is_bullish = True
                fresh_anchor_idx = int(macro_min_idx)
                fresh_anchor_time = int(dates[macro_min_idx])
                fresh_anchor_price = macro_low_val
            else:
                is_bullish = False
                fresh_anchor_idx = int(macro_max_idx)
                fresh_anchor_time = int(dates[macro_max_idx])
                fresh_anchor_price = macro_high_val

    if cycle_events:
        last_ev = cycle_events[-1]
        if last_ev["anchor_idx"] > max(macro_max_idx, macro_min_idx) - 15:
            is_bullish = last_ev["bullish"]
            fresh_anchor_idx = last_ev["anchor_idx"]
            fresh_anchor_time = last_ev["anchor_time"]
            fresh_anchor_price = last_ev["anchor_price"]

    if is_bullish:
        shoulder_win = max(0, fresh_anchor_idx - 65)
        true_leg_min_idx = shoulder_win + int(np.argmin(lows[shoulder_win : fresh_anchor_idx + 1]))
        if lows[true_leg_min_idx] < fresh_anchor_price:
            fresh_anchor_idx = int(true_leg_min_idx)
            fresh_anchor_time = int(dates[fresh_anchor_idx])
            fresh_anchor_price = float(lows[fresh_anchor_idx])
    else:
        shoulder_win = max(0, fresh_anchor_idx - 65)
        true_leg_max_idx = shoulder_win + int(np.argmax(highs[shoulder_win : fresh_anchor_idx + 1]))
        if highs[true_leg_max_idx] > fresh_anchor_price:
            fresh_anchor_idx = int(true_leg_max_idx)
            fresh_anchor_time = int(dates[fresh_anchor_idx])
            fresh_anchor_price = float(highs[fresh_anchor_idx])

    anchor_idx = fresh_anchor_idx
    anchor_time = fresh_anchor_time
    anchor_price = fresh_anchor_price

    if is_bullish:
        post_min_idx = anchor_idx + int(np.argmin(lows[anchor_idx:]))
        if lows[post_min_idx] < anchor_price:
            anchor_idx = post_min_idx
            anchor_time = int(dates[anchor_idx])
            anchor_price = float(lows[post_min_idx])
        move = float(highs[anchor_idx:].max() - anchor_price)
        extreme_held = bool(lows[anchor_idx:].min() >= anchor_price - 1e-9)
    else:
        post_max_idx = anchor_idx + int(np.argmax(highs[anchor_idx:]))
        if highs[post_max_idx] > anchor_price:
            anchor_idx = post_max_idx
            anchor_time = int(dates[post_max_idx])
            anchor_price = float(highs[post_max_idx])
        move = float(anchor_price - lows[anchor_idx:].min())
        extreme_held = bool(highs[anchor_idx:].max() <= anchor_price + 1e-9)

    peak_confirmed = bool(extreme_held and move >= ANCHOR_MIN_MOVE_ATR * atr)

    struct_events = find_choch_and_bos_from_peak(
        df, is_bullish, anchor_idx, raw_highs, raw_lows, atr, decimals
    )

    if not is_bullish:
        pattern_name = f"Peak Formation High ({anchor_price:.{decimals}f})"
        anchor_color = "rgba(255, 59, 59, 0.95)"
    else:
        pattern_name = f"Peak Formation Low ({anchor_price:.{decimals}f})"
        anchor_color = "rgba(59, 255, 130, 0.95)"

    all_lines = [{
        "level": round(anchor_price, decimals),
        "start_time": anchor_time,
        "end_time": int(dates[-1]),
        "type": pattern_name,
        "color": anchor_color,
        "is_anchor": True,
        "is_struct": False,
    }]

    for ev in struct_events:
        arrow = "▲" if is_bullish else "▼"
        is_c = (ev["kind"] == "CHoCH")
        col = (
            "rgba(0, 230, 118, 0.95)" if (is_bullish and is_c)
            else "rgba(38, 166, 154, 0.95)" if is_bullish
            else "rgba(255, 82, 82, 0.95)" if is_c
            else "rgba(239, 83, 80, 0.95)"
        )
        all_lines.append({
            "level": ev["level"],
            "start_time": ev["start_time"],
            "end_time": ev["end_time"],
            "type": f"{ev['kind']} {arrow} ({ev['level']:.{decimals}f})",
            "color": col,
            "is_anchor": False,
            "is_struct": True,
            "is_choch": is_c,
        })

    live_ref = current_price if current_price > 0 else float(closes[-1])

    sharkfin_data = detect_tdi_sharkfin_stophunt(
        df, tdi_arrays, is_bullish, int(anchor_idx), struct_events, live_ref, atr, decimals, pip_size
    )
    if sharkfin_data.get("pool_line"):
        all_lines.append(sharkfin_data["pool_line"])

    tdi_series = build_tdi_visual_series(
        df, tdi_arrays, fin_apex_map=sharkfin_data.get("fin_apex_map"), max_bars=500
    )

    pool_level = sharkfin_data.get("pool_level") or round(live_ref, decimals)
    est_sl_dist = 0.40 * atr
    targets = compute_pip_based_target(df, is_bullish, pool_level, est_sl_dist, int(anchor_idx), profile)

    projection_path = []
    if sharkfin_data.get("has_pool") and len(targets) >= 1:
        target_line_color = "rgba(255, 193, 7, 0.95)"
        t = targets[0]
        all_lines.append({
            "level": t["level"],
            "start_time": t["start_time"],
            "end_time": t["end_time"],
            "type": t["label"],
            "color": target_line_color,
            "is_anchor": False,
            "is_struct": False,
        })

        if not sharkfin_data.get("swept"):
            step_seconds = int(dates[-1] - dates[-2]) if len(dates) >= 2 else 3600
            projection_path = [
                {"time": int(dates[-1]), "price": round(live_ref, decimals), "label": "Now"},
                {"time": int(dates[-1] + step_seconds * 4), "price": round(pool_level, decimals), "label": "TDI Sharkfin Zone"},
                {"time": int(dates[-1] + step_seconds * 12), "price": t["level"], "label": t["label"]},
            ]

    if struct_events:
        latest_ev = struct_events[-1]
        break_kind = latest_ev["kind"]
        break_level = latest_ev["level"]
    else:
        break_kind = "CHoCH"
        break_level = round(anchor_price, decimals)

    if is_bullish:
        cycle = f"BULLISH TREND (Peak W + {break_kind} ▲)"
    else:
        cycle = f"BEARISH TREND (Peak M + {break_kind} ▼)"

    return {
        "cycle": cycle,
        "is_bullish": is_bullish,
        "break_kind": break_kind,
        "break_level": break_level,
        "struct_events": struct_events,
        "lines": all_lines,
        "anchor": round(anchor_price, decimals),
        "anchor_idx": int(anchor_idx),
        "atr": atr,
        "profile": profile,
        "peak_confirmed": peak_confirmed,
        "sweeps": [],
        "swing_order": swing_order,
        "raw_highs": raw_highs.tolist(),
        "raw_lows": raw_lows.tolist(),
        "trendline": None,
        "sharkfin_data": sharkfin_data,
        "tdi_series": tdi_series,
        "zone_boxes": [],
        "poi_level": round(pool_level, decimals),
        "targets": targets,
        "projection_path": projection_path,
    }


def evaluate_entry_signal(df: pd.DataFrame, full_df: pd.DataFrame, ms: Dict, current_price: float,
                          ema_50: float, ema_200: float, rsi: float,
                          htf_aligned: bool, decimals: int, pip_size: float,
                          state_key: Optional[str] = None) -> Dict:
    is_bullish = ms["is_bullish"]
    peak_confirmed = ms["peak_confirmed"]
    anchor_price = ms["anchor"]
    anchor_idx = ms["anchor_idx"]
    atr = ms["atr"]
    profile = ms.get("profile", {"decimals": decimals, "pip_size": pip_size, "target_pips": FOREX_TP_PIPS})
    break_kind = ms["break_kind"]
    break_level = ms["break_level"]
    sharkfin_data = ms.get("sharkfin_data", {})
    targets = ms["targets"]

    closes = df['Close'].values
    highs = df['High'].values
    lows = df['Low'].values
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(closes)

    has_pool = bool(sharkfin_data.get("has_pool", False))
    watching_fin = sharkfin_data.get("watching_fin")
    pool_level = sharkfin_data.get("pool_level")
    recent_sweeps = sharkfin_data.get("recent_sweeps", [])
    live_tdi = sharkfin_data.get("live_tdi", {"green": 50.0, "red": 50.0, "yellow": 50.0, "upper": 68.0, "lower": 32.0})

    tp_level = targets[0]["level"] if len(targets) >= 1 else None
    base_tp_pips = profile.get("target_pips", FOREX_TP_PIPS)

    dir_word = "BULLISH (BUY BIAS)" if is_bullish else "BEARISH (SELL BIAS)"
    peak_word = "Peak Low (W)" if is_bullish else "Peak High (M)"
    arrow = "▲" if is_bullish else "▼"
    min_be_buf = round(max(pip_size * 2.0, 0.04 * atr), decimals)

    if state_key and state_key not in CLOSED_SIGNALS:
        CLOSED_SIGNALS[state_key] = set()

    if state_key and state_key in ACTIVE_TRADES:
        lt = ACTIVE_TRADES[state_key]
        live_high = max(float(full_df['High'].iloc[-1]), current_price)
        live_low = min(float(full_df['Low'].iloc[-1]), current_price)

        init_sl = lt.get("initial_sl", lt["stop_loss"])
        init_risk = max(abs(lt["entry"] - init_sl), 1e-6)
        tp_dist = abs(lt["target_1"] - lt["entry"])
        be_trigger_dist = max(init_risk * 1.25, 0.65 * tp_dist)
        be_cushion = round(max(min_be_buf, 0.25 * init_risk), decimals)

        if not lt.get("breakeven_active", False):
            if lt["is_bullish"] and (live_high - lt["entry"]) >= be_trigger_dist:
                lt["stop_loss"] = round(lt["entry"] + be_cushion, decimals)
                lt["breakeven_active"] = True
            elif (not lt["is_bullish"]) and (lt["entry"] - live_low) >= be_trigger_dist:
                lt["stop_loss"] = round(lt["entry"] - be_cushion, decimals)
                lt["breakeven_active"] = True

        closed_out = False
        if lt["is_bullish"] != is_bullish:
            closed_out = True
        elif lt["is_bullish"] and (live_low <= lt["stop_loss"] or live_high >= lt["target_1"]):
            closed_out = True
        elif (not lt["is_bullish"]) and (live_high >= lt["stop_loss"] or live_low <= lt["target_1"]):
            closed_out = True

        if closed_out:
            if lt.get("signal_time"):
                CLOSED_SIGNALS[state_key].add(int(lt["signal_time"]))
            del ACTIVE_TRADES[state_key]

    locked_trade = ACTIVE_TRADES.get(state_key) if state_key else None

    if locked_trade is None and watching_fin is None and peak_confirmed and recent_sweeps:
        for sw in recent_sweeps:
            k = sw["signal_idx"]
            sig_ts = sw["signal_time"]
            if (total - 1 - k) > 35:
                continue
            if state_key and sig_ts in CLOSED_SIGNALS.get(state_key, set()):
                continue

            entry_k = sw["entry_close"]
            sweep_ext = sw["sweep_extreme"]

            if is_bullish:
                init_sl_k = round(min(sweep_ext - 0.18 * atr, entry_k - 0.30 * atr), decimals)
                risk_k = max(abs(entry_k - init_sl_k), 1e-6)

                t_list = compute_pip_based_target(df.iloc[:k + 1], True, entry_k, risk_k, anchor_idx, profile)
                tp_k = t_list[0]["level"]
                tp_pips_k = t_list[0]["pips"]
                tp_dist_k = abs(tp_k - entry_k)
                rr_k = round(tp_dist_k / risk_k, 2)

                be_trigger_k = max(risk_k * 1.25, 0.65 * tp_dist_k)
                be_cushion_k = round(max(min_be_buf, 0.25 * risk_k), decimals)
                curr_sl_k = init_sl_k
                be_active_k = False
                trade_closed = False

                sim_highs = list(highs[k + 1:]) + [max(float(full_df['High'].iloc[-1]), current_price)]
                sim_lows = list(lows[k + 1:]) + [min(float(full_df['Low'].iloc[-1]), current_price)]

                for s_hi, s_lo in zip(sim_highs, sim_lows):
                    if float(s_lo) <= curr_sl_k or float(s_hi) >= tp_k:
                        trade_closed = True
                        break
                    if (not be_active_k) and (float(s_hi) - entry_k) >= be_trigger_k:
                        curr_sl_k = round(entry_k + be_cushion_k, decimals)
                        be_active_k = True

                if trade_closed:
                    if state_key:
                        CLOSED_SIGNALS[state_key].add(sig_ts)
                    continue

                locked_trade = {
                    "is_bullish": True,
                    "signal": "BUY",
                    "signal_idx": k,
                    "signal_time": sig_ts,
                    "entry": entry_k,
                    "initial_sl": init_sl_k,
                    "stop_loss": curr_sl_k,
                    "breakeven_active": be_active_k,
                    "target_1": tp_k,
                    "tp_pips": tp_pips_k,
                    "risk_reward": rr_k,
                    "pool_level": sw["pool_level"],
                    "pool_type": sw["pool_type"],
                    "pattern": sw["pattern"],
                }
                if state_key:
                    ACTIVE_TRADES[state_key] = locked_trade
                break

            else:
                init_sl_k = round(max(sweep_ext + 0.18 * atr, entry_k + 0.30 * atr), decimals)
                risk_k = max(abs(init_sl_k - entry_k), 1e-6)

                t_list = compute_pip_based_target(df.iloc[:k + 1], False, entry_k, risk_k, anchor_idx, profile)
                tp_k = t_list[0]["level"]
                tp_pips_k = t_list[0]["pips"]
                tp_dist_k = abs(entry_k - tp_k)
                rr_k = round(tp_dist_k / risk_k, 2)

                be_trigger_k = max(risk_k * 1.25, 0.65 * tp_dist_k)
                be_cushion_k = round(max(min_be_buf, 0.25 * risk_k), decimals)
                curr_sl_k = init_sl_k
                be_active_k = False
                trade_closed = False

                sim_highs = list(highs[k + 1:]) + [max(float(full_df['High'].iloc[-1]), current_price)]
                sim_lows = list(lows[k + 1:]) + [min(float(full_df['Low'].iloc[-1]), current_price)]

                for s_hi, s_lo in zip(sim_highs, sim_lows):
                    if float(s_hi) >= curr_sl_k or float(s_lo) <= tp_k:
                        trade_closed = True
                        break
                    if (not be_active_k) and (entry_k - float(s_lo)) >= be_trigger_k:
                        curr_sl_k = round(entry_k - be_cushion_k, decimals)
                        be_active_k = True

                if trade_closed:
                    if state_key:
                        CLOSED_SIGNALS[state_key].add(sig_ts)
                    continue

                locked_trade = {
                    "is_bullish": False,
                    "signal": "SELL",
                    "signal_idx": k,
                    "signal_time": sig_ts,
                    "entry": entry_k,
                    "initial_sl": init_sl_k,
                    "stop_loss": curr_sl_k,
                    "breakeven_active": be_active_k,
                    "target_1": tp_k,
                    "tp_pips": tp_pips_k,
                    "risk_reward": rr_k,
                    "pool_level": sw["pool_level"],
                    "pool_type": sw["pool_type"],
                    "pattern": sw["pattern"],
                }
                if state_key:
                    ACTIVE_TRADES[state_key] = locked_trade
                break

    if locked_trade:
        f_entry = locked_trade["entry"]
        f_sl = locked_trade["stop_loss"]
        f_init_sl = locked_trade.get("initial_sl", f_sl)
        f_be = bool(locked_trade.get("breakeven_active", False))
        f_tp = locked_trade["target_1"]
        f_tp_pips = locked_trade.get("tp_pips", base_tp_pips)
        f_rr = locked_trade["risk_reward"]
        f_dir = locked_trade["signal"]
        f_pool_lvl = locked_trade.get("pool_level", f_entry)
        f_pat = locked_trade.get("pattern", "Confirmed TDI Sharkfin")
        f_ptype = locked_trade.get("pool_type", "TDI Sharkfin")

        # Update chart target line to match the locked trade's exact Pip TP
        for line in ms["lines"]:
            if line.get("type", "").startswith("Target"):
                line["level"] = f_tp
                line["type"] = f"Target +{f_tp_pips}p ({f_tp:.{decimals}f})"

        step_seconds = int(dates[-1] - dates[-2]) if len(dates) >= 2 else 3600
        now_ts = int(full_df['Date'].iloc[-1]) if 'Date' in full_df.columns else int(dates[-1])
        live_p = round(current_price if current_price > 0 else float(closes[-1]), decimals)
        mid_p = round((live_p + f_tp) / 2.0, decimals)
        ms["projection_path"] = [
            {"time": now_ts, "price": live_p, "label": "Now"},
            {"time": now_ts + step_seconds * 4, "price": mid_p, "label": "Path"},
            {"time": now_ts + step_seconds * 8, "price": f_tp, "label": "TP"},
        ]

        sl_status_txt = f"SL @ BE ({f_sl:.{decimals}f}) 🛡️" if f_be else f"TP: +{f_tp_pips}p (RR 1:{f_rr}) ✅"
        checklist = [
            {"step": f"1. Peak Anchor ({peak_word})", "done": True, "detail": f"Locked @ {anchor_price:.{decimals}f} ✅"},
            {"step": f"2. Structure ({break_kind} {arrow})", "done": True, "detail": f"Cleared {break_level:.{decimals}f} ✅"},
            {"step": f"3. {f_ptype}", "done": True, "detail": f"Swept {f_pool_lvl:.{decimals}f} 🦈✅"},
            {"step": f"4. Base Line Close & +{f_tp_pips}p TP", "done": True, "detail": sl_status_txt},
        ]

        active_conf = 88 + (4 if htf_aligned else 0) + (2 if f_be else 0)
        sl_label = f"SL (BE 🛡️): {f_sl:.{decimals}f}" if f_be else f"SL: {f_sl:.{decimals}f}"
        trigger_note = (
            f"{'🟢 BUY' if is_bullish else '🔴 SELL'} EXECUTING ({f_pat}): Locked Entry @ {f_entry:.{decimals}f} | "
            f"TP (+{f_tp_pips}p): {f_tp:.{decimals}f} | {sl_label}"
        )

        return {
            "signal": f_dir,
            "confidence": min(96, active_conf),
            "setup_name": f"Confirmed Sharkfin {f_dir}" + (" (Risk-Free BE 🛡️)" if f_be else ""),
            "checklist": checklist,
            "execution_plan": {
                "stage": "TRADE_ACTIVE",
                "trigger_condition": trigger_note,
                "planned_entry": f_entry,
                "planned_sl": f_sl,
                "initial_sl": f_init_sl,
                "breakeven_active": f_be,
                "planned_tp1": f_tp,
                "planned_tp2": None,
                "risk_reward": f_rr,
            },
            "entry_note": trigger_note,
            "trade_levels": {
                "entry": f_entry,
                "stop_loss": f_sl,
                "initial_sl": f_init_sl,
                "breakeven_active": f_be,
                "take_profit": f_tp,
                "target_1": f_tp,
                "target_2": None,
                "risk_reward": f_rr,
            }
        }

    g_now = live_tdi["green"]
    y_now = live_tdi["yellow"]
    u_now = live_tdi["upper"]
    l_now = live_tdi["lower"]

    if is_bullish:
        ref_lvl = pool_level if pool_level is not None else round(current_price, decimals)
        planned_sl = round(ref_lvl - (0.35 * atr), decimals)
        risk = max(abs(ref_lvl - planned_sl), 1e-6)
        planned_t_list = compute_pip_based_target(df, True, ref_lvl, risk, anchor_idx, profile)
        planned_tp = planned_t_list[0]["level"]
        planned_pips = planned_t_list[0]["pips"]
        best_rr = round(abs(planned_tp - ref_lvl) / risk, 2)

        if watching_fin is not None:
            stage = "IN_ZONE_CONFIRMING"
            note = (
                f"👀 POTENTIAL BUY STOP-HUNT DETECTED (TDI V-Fin @ {watching_fin['tdi_val']})! "
                f"Watching until TDI Green ({g_now}) closes ABOVE the Yellow Base Line ({y_now}) to confirm BUY (+{planned_pips}p TP). "
                f"(Invalidated if Green dips into a W-shape!)"
            )
            checklist = [
                {"step": f"1. Peak Anchor ({peak_word})", "done": bool(peak_confirmed), "detail": f"Locked @ {anchor_price:.{decimals}f} ✅"},
                {"step": f"2. Structure ({break_kind} {arrow})", "done": True, "detail": f"Cleared {break_level:.{decimals}f} ✅"},
                {"step": "3. Lower-Band V-Fin Stop Hunt", "done": True, "detail": f"V-Fin @ {watching_fin['tdi_val']} 🦈👀"},
                {"step": f"4. Close ABOVE Base Line (+{planned_pips}p TP)", "done": False, "detail": f"Green {g_now} -> Need > {y_now}"},
            ]
            return {
                "signal": "WAIT",
                "confidence": 76,
                "setup_name": "Potential Buy Stop-Hunt — Await Base Line Close 👀",
                "checklist": checklist,
                "execution_plan": {
                    "stage": stage,
                    "trigger_condition": note,
                    "planned_entry": round(current_price, decimals),
                    "planned_sl": round(watching_fin["sweep_extreme"] - 0.18 * atr, decimals),
                    "planned_tp1": planned_tp,
                    "planned_tp2": None,
                    "risk_reward": best_rr,
                },
                "entry_note": note,
                "trade_levels": None,
            }

        checklist = [
            {"step": f"1. Peak Anchor ({peak_word})", "done": bool(peak_confirmed), "detail": f"Locked @ {anchor_price:.{decimals}f} ✅"},
            {"step": f"2. Structure ({break_kind} {arrow})", "done": True, "detail": f"Cleared {break_level:.{decimals}f} ✅"},
            {"step": "3. Lower-Band V-Fin Stop Hunt", "done": False, "detail": f"Green: {g_now} | Lower Band: {l_now} ⏳"},
            {"step": f"4. Close ABOVE Base Line (+{base_tp_pips}p TP)", "done": False, "detail": "Wait for Clean V-Fin First"},
        ]
        note = (
            f"⏳ WATCHING FOR BUY STOP-HUNT: {dir_word} from {peak_word} ({anchor_price:.{decimals}f}). "
            f"Waiting for TDI Green ({g_now}) to form a sharp single V-fin at the Lower Blue Band ({l_now}) and close ABOVE the Yellow Base Line ({y_now}) for a +{base_tp_pips}p TP."
        )
        return {
            "signal": "WAIT",
            "confidence": 58,
            "setup_name": "Watching for Buy Stop-Hunt (PFL Bias)",
            "checklist": checklist,
            "execution_plan": {
                "stage": "AWAITING_PULLBACK",
                "trigger_condition": note,
                "planned_entry": ref_lvl if has_pool else None,
                "planned_sl": planned_sl if has_pool else None,
                "planned_tp1": planned_tp if has_pool else None,
                "planned_tp2": None,
                "risk_reward": best_rr,
            },
            "entry_note": note,
            "trade_levels": None,
        }

    else:
        ref_lvl = pool_level if pool_level is not None else round(current_price, decimals)
        planned_sl = round(ref_lvl + (0.35 * atr), decimals)
        risk = max(abs(planned_sl - ref_lvl), 1e-6)
        planned_t_list = compute_pip_based_target(df, False, ref_lvl, risk, anchor_idx, profile)
        planned_tp = planned_t_list[0]["level"]
        planned_pips = planned_t_list[0]["pips"]
        best_rr = round(abs(ref_lvl - planned_tp) / risk, 2)

        if watching_fin is not None:
            stage = "IN_ZONE_CONFIRMING"
            note = (
                f"👀 POTENTIAL SELL STOP-HUNT DETECTED (TDI V-Fin @ {watching_fin['tdi_val']})! "
                f"Watching until TDI Green ({g_now}) closes BELOW the Yellow Base Line ({y_now}) to confirm SELL (+{planned_pips}p TP). "
                f"(Invalidated if Green bounces into an M-shape!)"
            )
            checklist = [
                {"step": f"1. Peak Anchor ({peak_word})", "done": bool(peak_confirmed), "detail": f"Locked @ {anchor_price:.{decimals}f} ✅"},
                {"step": f"2. Structure ({break_kind} {arrow})", "done": True, "detail": f"Cleared {break_level:.{decimals}f} ✅"},
                {"step": "3. Upper-Band V-Fin Stop Hunt", "done": True, "detail": f"V-Fin @ {watching_fin['tdi_val']} 🦈👀"},
                {"step": f"4. Close BELOW Base Line (+{planned_pips}p TP)", "done": False, "detail": f"Green {g_now} -> Need < {y_now}"},
            ]
            return {
                "signal": "WAIT",
                "confidence": 76,
                "setup_name": "Potential Sell Stop-Hunt — Await Base Line Close 👀",
                "checklist": checklist,
                "execution_plan": {
                    "stage": stage,
                    "trigger_condition": note,
                    "planned_entry": round(current_price, decimals),
                    "planned_sl": round(watching_fin["sweep_extreme"] + 0.18 * atr, decimals),
                    "planned_tp1": planned_tp,
                    "planned_tp2": None,
                    "risk_reward": best_rr,
                },
                "entry_note": note,
                "trade_levels": None,
            }

        checklist = [
            {"step": f"1. Peak Anchor ({peak_word})", "done": bool(peak_confirmed), "detail": f"Locked @ {anchor_price:.{decimals}f} ✅"},
            {"step": f"2. Structure ({break_kind} {arrow})", "done": True, "detail": f"Cleared {break_level:.{decimals}f} ✅"},
            {"step": "3. Upper-Band V-Fin Stop Hunt", "done": False, "detail": f"Green: {g_now} | Upper Band: {u_now} ⏳"},
            {"step": f"4. Close BELOW Base Line (+{base_tp_pips}p TP)", "done": False, "detail": "Wait for Clean V-Fin First"},
        ]
        note = (
            f"⏳ WATCHING FOR SELL STOP-HUNT: {dir_word} from {peak_word} ({anchor_price:.{decimals}f}). "
            f"Waiting for TDI Green ({g_now}) to form a sharp single V-fin at the Upper Blue Band ({u_now}) and close BELOW the Yellow Base Line ({y_now}) for a +{base_tp_pips}p TP."
        )
        return {
            "signal": "WAIT",
            "confidence": 58,
            "setup_name": "Watching for Sell Stop-Hunt (PFH Bias)",
            "checklist": checklist,
            "execution_plan": {
                "stage": "AWAITING_PULLBACK",
                "trigger_condition": note,
                "planned_entry": ref_lvl if has_pool else None,
                "planned_sl": planned_sl if has_pool else None,
                "planned_tp1": planned_tp if has_pool else None,
                "planned_tp2": None,
                "risk_reward": best_rr,
            },
            "entry_note": note,
            "trade_levels": None,
        }


@app.post("/api/analyze")
async def analyze(req: AnalysisRequest):
    if req.candles and len(req.candles) > 50:
        df = process_live_candles(req.candles)
        data_source = "LIVE_NODE_DATA"
    else:
        df = MARKET_MEMORY["df"]
        data_source = "CSV_FALLBACK"

    if df is None or df.empty:
        return {"signal": "HOLD", "confidence": 0, "reasoning": ["⏳ Waiting for data..."]}

    try:
        csv_last_price = safe_float(df['Close'].iloc[-1])
        current_price = req.current_price if req.current_price > 0 else csv_last_price

        profile = get_instrument_profile(req.currency, current_price)
        decimals = profile['decimals']
        pip_size = profile['pip_size']

        ema_200 = safe_float(EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1], current_price)
        ema_50 = safe_float(EMAIndicator(close=df['Close'], window=50).ema_indicator().iloc[-1], current_price)
        rsi = safe_float(RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1], 50.0)

        state_key = f"{req.currency}:{req.timeframe}"
        ms = analyze_market_structure(
            df, profile,
            current_price=current_price,
            state_key=state_key,
            drop_forming=False,
        )
        cycle = ms['cycle']
        lines = ms['lines']
        peak_confirmed = ms.get('peak_confirmed', False)
        targets = ms.get('targets', [])

        bias_str = 'BULLISH' if ms['is_bullish'] else 'BEARISH'

        htf_aligned = False
        htf_bias_str = "NEUTRAL"
        if req.htf_candles and len(req.htf_candles) > 50:
            htf_df = process_live_candles(req.htf_candles)
            if htf_df is not None:
                htf_ema50 = safe_float(EMAIndicator(close=htf_df['Close'], window=50).ema_indicator().iloc[-1])
                htf_ema200 = safe_float(EMAIndicator(close=htf_df['Close'], window=200).ema_indicator().iloc[-1])
                htf_bias_str = "BULLISH" if htf_ema50 > htf_ema200 else "BEARISH"
                htf_aligned = (htf_bias_str == bias_str)
        else:
            htf_aligned = (ms['is_bullish'] and ema_50 > ema_200) or ((not ms['is_bullish']) and ema_50 < ema_200)
            htf_bias_str = bias_str if htf_aligned else "NEUTRAL"

        eval_df = df.iloc[:-1].reset_index(drop=True) if (DROP_FORMING_CANDLE and data_source == "LIVE_NODE_DATA" and len(df) > 60) else df
        entry_eval = evaluate_entry_signal(
            eval_df, df, ms, current_price, ema_50, ema_200, rsi, htf_aligned, decimals, pip_size, state_key=state_key
        )

        checklist = entry_eval.get("checklist", [])
        reasoning = [
            f"{'✅' if c['done'] else '⏳'} {c['step']} ({c['detail']})"
            for c in checklist
        ]
        reasoning.append(entry_eval["entry_note"])

        has_valid_pool = bool(ms.get("sharkfin_data", {}).get("has_pool")) or (entry_eval["trade_levels"] is not None)
        t1_out = (
            entry_eval["trade_levels"]["target_1"] if entry_eval["trade_levels"]
            else (targets[0]["level"] if (has_valid_pool and len(targets) >= 1) else None)
        )

        return {
            "signal": entry_eval["signal"],
            "confidence": entry_eval["confidence"],
            "trend": cycle,
            "pattern": entry_eval["setup_name"],
            "peak_confirmed": peak_confirmed,
            "checklist": checklist,
            "executionPlan": entry_eval["execution_plan"],
            "reasoning": reasoning,
            "keyLevels": {
                "resistance": round(ms["anchor"], decimals) if not ms["is_bullish"] else round(ms["break_level"], decimals),
                "support": round(ms["anchor"], decimals) if ms["is_bullish"] else round(ms["break_level"], decimals),
                "poi": ms["poi_level"] if has_valid_pool else None,
                "target1": t1_out,
                "target2": None,
                "ema200": round(ema_200, decimals),
                "ema50": round(ema_50, decimals),
            },
            "visuals": {
                "bos_lines": lines,
                "sweeps": [],
                "trendline": None,
                "order_blocks": [],
                "targets": targets if has_valid_pool else [],
                "projection_path": ms["projection_path"],
                "tdi_data": ms.get("tdi_series", []),
            },
            "mtf_confluence": [
                {"tf": "4H", "bias": htf_bias_str, "strength": 88 if htf_aligned else 50},
                {"tf": "1H", "bias": bias_str, "strength": 85 if peak_confirmed else 60},
            ],
            "tradeSetup": entry_eval["trade_levels"],
            "dataSource": data_source,
        }

    except Exception as e:
        logger.error(f"Analysis Crash: {e}", exc_info=True)
        return {"signal": "ERROR", "confidence": 0, "reasoning": [f"Engine error: {str(e)}"]}


@app.post("/api/debug")
async def debug_analysis(req: AnalysisRequest):
    if req.candles and len(req.candles) > 50:
        df = process_live_candles(req.candles)
    else:
        df = MARKET_MEMORY["df"]

    if df is None or df.empty:
        return {"error": "No data available"}

    try:
        current_price = req.current_price if req.current_price > 0 else safe_float(df['Close'].iloc[-1])
        profile = get_instrument_profile(req.currency, current_price)
        ms = analyze_market_structure(df, profile, current_price=current_price)
        return {
            "✅ ENGINE VERSION": "AuraBrain v12.5 — Zero-M/W Sharkfin Filter + Pip-Based TP (50p Forex / 150p Gold, 2.0R Floor)",
            "📊 INSTRUMENT": req.currency,
            "🎯 STRUCTURE": ms['cycle'],
            "⚓ PEAK ANCHOR": ms['anchor'],
            "📏 LINES": ms['lines'],
            "🦈 TDI SHARKFIN DATA": ms.get('sharkfin_data'),
            "🎯 TARGETS": ms.get('targets', []),
            "🔒 ACTIVE TRADES": ACTIVE_TRADES,
            "🚫 CLOSED SIGNALS": {k: list(v) for k, v in CLOSED_SIGNALS.items()},
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_to_node(path: str, request: Request):
    try:
        url = f"{NODE_URL}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        body = await request.body() if request.method in ["POST", "PUT"] else None
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
            headers=dict(response.headers)
        )
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        raise HTTPException(status_code=502, detail="Node backend unreachable")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)