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
MIN_FVG_SIZE_ATR = 0.05
MIN_ALLOWED_RR = 1.5

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
        return {"decimals": 2, "pip_size": 0.1, "label": "XAU/USD"}
    if cu in ("XAGUSD", "SILVER"):
        return {"decimals": 3, "pip_size": 0.001, "label": "XAG/USD"}
    if "JPY" in cu:
        return {"decimals": 3, "pip_size": 0.01, "label": currency}
    if any(cu.startswith(p) or cu.endswith(p) for p in ("EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "USD")):
        return {"decimals": 5, "pip_size": 0.0001, "label": currency}
    if current_price > 500:
        return {"decimals": 2, "pip_size": 0.1, "label": currency}
    if current_price > 20:
        return {"decimals": 3, "pip_size": 0.01, "label": currency}
    return {"decimals": 5, "pip_size": 0.0001, "label": currency}


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


def evaluate_pre_break_candle_poi(df: pd.DataFrame, is_bullish: bool, break_idx: int,
                                  top_val: float, bot_val: float,
                                  current_price: float, atr: float) -> str:
    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values
    total = len(closes)

    if break_idx >= total - 1:
        return "INVALIDATED"

    depart_idx = break_idx
    if is_bullish:
        while depart_idx + 1 < total - 1 and lows[depart_idx + 1] <= top_val and closes[depart_idx + 1] > top_val:
            if (highs[depart_idx] - top_val) >= (0.45 * atr):
                break
            depart_idx += 1
    else:
        while depart_idx + 1 < total - 1 and highs[depart_idx + 1] >= bot_val and closes[depart_idx + 1] < bot_val:
            if (bot_val - lows[depart_idx]) >= (0.45 * atr):
                break
            depart_idx += 1

    if depart_idx >= total - 1:
        return "INVALIDATED"

    if is_bullish:
        first_tap = None
        for idx in range(depart_idx + 1, total):
            if lows[idx] < bot_val - (0.08 * atr) or closes[idx] < bot_val:
                return "INVALIDATED"
            if lows[idx] <= top_val + (0.02 * atr) and first_tap is None:
                first_tap = idx

        if current_price < bot_val - (0.08 * atr):
            return "INVALIDATED"

        if first_tap is None:
            if current_price <= top_val + (0.02 * atr):
                return "TAPPED_ACTIVE"
            return "UNTOUCHED"

        pre_tap_high = float(np.max(highs[break_idx : first_tap + 1]))
        if (pre_tap_high - top_val) < (0.30 * atr):
            return "INVALIDATED"

        if first_tap + 1 < total:
            post_tap_high = max(float(np.max(highs[first_tap + 1:])), current_price)
            if post_tap_high >= pre_tap_high + (0.05 * atr):
                return "INVALIDATED"

        return "TAPPED_ACTIVE"

    else:
        first_tap = None
        for idx in range(depart_idx + 1, total):
            if highs[idx] > top_val + (0.08 * atr) or closes[idx] > top_val:
                return "INVALIDATED"
            if highs[idx] >= bot_val - (0.02 * atr) and first_tap is None:
                first_tap = idx

        if current_price > top_val + (0.08 * atr):
            return "INVALIDATED"

        if first_tap is None:
            if current_price >= bot_val - (0.02 * atr):
                return "TAPPED_ACTIVE"
            return "UNTOUCHED"

        pre_tap_low = float(np.min(lows[break_idx : first_tap + 1]))
        if (bot_val - pre_tap_low) < (0.30 * atr):
            return "INVALIDATED"

        if first_tap + 1 < total:
            post_tap_low = min(float(np.min(lows[first_tap + 1:])), current_price)
            if post_tap_low <= pre_tap_low - (0.05 * atr):
                return "INVALIDATED"

        return "TAPPED_ACTIVE"


def find_or_lock_unmitigated_poi(df: pd.DataFrame, is_bullish: bool, anchor_idx: int,
                                 anchor_time: int, struct_events: List[Dict],
                                 current_price: float, atr: float, decimals: int,
                                 state_key: Optional[str] = None) -> Optional[Dict]:
    opens = df['Open'].values
    closes = df['Close'].values
    highs = df['High'].values
    lows = df['Low'].values
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(closes)

    if not struct_events:
        return None

    min_candle_span = 0.14 * atr

    for ev_idx in range(len(struct_events) - 1, -1, -1):
        ev = struct_events[ev_idx]
        orig_idx = int(np.clip(ev.get("leg_origin", anchor_idx), anchor_idx, total - 3))
        break_idx = int(np.clip(ev.get("break_idx", total - 1), orig_idx + 1, total - 2))

        if ev_idx < len(struct_events) - 1:
            subsequent_broken = False
            for next_ev in struct_events[ev_idx + 1:]:
                n_brk = int(next_ev.get("break_idx", total - 1))
                n_orig = int(next_ev.get("leg_origin", anchor_idx))
                if n_brk + 2 < total:
                    if is_bullish:
                        orig_floor = float(lows[n_orig]) if 0 <= n_orig < total else float(next_ev["level"])
                        if float(np.min(lows[n_brk + 1:])) < orig_floor - (0.25 * atr):
                            subsequent_broken = True
                            break
                    else:
                        orig_ceil = float(highs[n_orig]) if 0 <= n_orig < total else float(next_ev["level"])
                        if float(np.max(highs[n_brk + 1:])) > orig_ceil + (0.25 * atr):
                            subsequent_broken = True
                            break
            if subsequent_broken:
                continue

        ob_idx = max(orig_idx, break_idx - 1)

        if is_bullish:
            bot_val = float(lows[ob_idx])
            top_val = max(float(highs[ob_idx]), bot_val + min_candle_span)

            status = evaluate_pre_break_candle_poi(
                df, True, break_idx, top_val, bot_val, current_price, atr
            )
            if status == "INVALIDATED":
                continue

            fvg_str = None
            has_fvg = False
            fvg_lo = max(orig_idx, break_idx - 3)
            fvg_hi = min(total - 3, break_idx + 1)
            for k in range(fvg_hi, fvg_lo - 1, -1):
                if (closes[k + 1] > opens[k + 1]) and (float(lows[k + 2] - highs[k]) >= MIN_FVG_SIZE_ATR * atr):
                    fvg_str = f"{round(float(highs[k]), decimals):.{decimals}f}–{round(float(lows[k + 2]), decimals):.{decimals}f}"
                    has_fvg = True
                    break

            bot_r = round(bot_val, decimals)
            top_r = round(top_val, decimals)
            prefix = "ACTIVE BUY POI" if status == "TAPPED_ACTIVE" else "BUY POI"

            poi_result = {
                "engine_ver": "11.2",
                "is_bullish": True,
                "status": status,
                "anchor_time": anchor_time,
                "ob_idx": int(ob_idx),
                "depart_idx": int(break_idx),
                "depart_time": int(dates[break_idx]),
                "top": top_r,
                "bottom": bot_r,
                "poi_level": top_r,
                "ob_range": f"{bot_r:.{decimals}f}–{top_r:.{decimals}f}",
                "has_fvg": has_fvg,
                "fvg_range": fvg_str if fvg_str else "Direct Break (No FVG)",
                "locked_idm": None,
                "box": {
                    "type": "BULL_OB",
                    "top": top_r,
                    "bottom": bot_r,
                    "time": int(dates[ob_idx]),
                    "end_time": int(dates[-1]),
                    "label": f"{prefix} ({bot_r:.{decimals}f}–{top_r:.{decimals}f})",
                    "mitigated": False,
                }
            }
            if state_key:
                POI_STATE[state_key] = poi_result
            return poi_result

        else:
            top_val = float(highs[ob_idx])
            bot_val = min(float(lows[ob_idx]), top_val - min_candle_span)

            status = evaluate_pre_break_candle_poi(
                df, False, break_idx, top_val, bot_val, current_price, atr
            )
            if status == "INVALIDATED":
                continue

            fvg_str = None
            has_fvg = False
            fvg_lo = max(orig_idx, break_idx - 3)
            fvg_hi = min(total - 3, break_idx + 1)
            for k in range(fvg_hi, fvg_lo - 1, -1):
                if (opens[k + 1] > closes[k + 1]) and (float(lows[k] - highs[k + 2]) >= MIN_FVG_SIZE_ATR * atr):
                    fvg_str = f"{round(float(highs[k + 2]), decimals):.{decimals}f}–{round(float(lows[k]), decimals):.{decimals}f}"
                    has_fvg = True
                    break

            bot_r = round(bot_val, decimals)
            top_r = round(top_val, decimals)
            prefix = "ACTIVE SELL POI" if status == "TAPPED_ACTIVE" else "SELL POI"

            poi_result = {
                "engine_ver": "11.2",
                "is_bullish": False,
                "status": status,
                "anchor_time": anchor_time,
                "ob_idx": int(ob_idx),
                "depart_idx": int(break_idx),
                "depart_time": int(dates[break_idx]),
                "top": top_r,
                "bottom": bot_r,
                "poi_level": bot_r,
                "ob_range": f"{bot_r:.{decimals}f}–{top_r:.{decimals}f}",
                "has_fvg": has_fvg,
                "fvg_range": fvg_str if fvg_str else "Direct Break (No FVG)",
                "locked_idm": None,
                "box": {
                    "type": "BEAR_OB",
                    "top": top_r,
                    "bottom": bot_r,
                    "time": int(dates[ob_idx]),
                    "end_time": int(dates[-1]),
                    "label": f"{prefix} ({bot_r:.{decimals}f}–{top_r:.{decimals}f})",
                    "mitigated": False,
                }
            }
            if state_key:
                POI_STATE[state_key] = poi_result
            return poi_result

    return None


def find_pullback_liquidity(df: pd.DataFrame, is_bullish: bool, poi_info: Optional[Dict],
                            atr: float, decimals: int) -> Dict:
    if not poi_info:
        return {"has_liquidity": False, "swept": False, "liq_type": "Inducement (IDM)", "liq_level": None, "liq_line": None}

    highs = df['High'].values
    lows = df['Low'].values
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(highs)

    dep_time = poi_info.get("depart_time", 0)
    dep_matches = np.where(dates >= dep_time)[0]
    depart_idx = int(dep_matches[0]) if len(dep_matches) > 0 else total

    if depart_idx >= total - 5:
        return {"has_liquidity": False, "swept": False, "liq_type": "Inducement (IDM)", "liq_level": None, "liq_line": None}

    poi_top = poi_info["top"]
    poi_bot = poi_info["bottom"]
    scan_end = total - 4

    if is_bullish:
        cand_lows = []
        for j in range(depart_idx + 2, scan_end):
            if (
                lows[j] <= lows[j - 1] and lows[j] <= lows[j - 2]
                and lows[j] <= lows[j + 1] and lows[j] <= lows[j + 2]
            ):
                subsequent_bounce = float(np.max(highs[j + 1 : min(total, j + 8)])) - float(lows[j])
                if subsequent_bounce >= (0.45 * atr):
                    if poi_bot + (0.05 * atr) <= lows[j] <= poi_top + (6.5 * atr):
                        cand_lows.append(j)

        if cand_lows:
            idm_idx = cand_lows[0]
            idm_price = float(lows[idm_idx])
            eq_lows = [idx for idx in cand_lows if abs(lows[idx] - idm_price) <= 0.20 * atr]
            liq_type = "Range Liquidity (EQL)" if len(eq_lows) >= 2 else "Inducement (IDM)"
            lvl_r = round(idm_price, decimals)

            post_min = float(np.min(lows[idm_idx + 1:])) if idm_idx + 1 < total else idm_price
            swept = bool(post_min < idm_price - (0.01 * atr))
            status_tag = "SWEPT ✅" if swept else ""
            label_str = f"$$$ {liq_type} {status_tag} ({lvl_r:.{decimals}f})".replace("  ", " ")

            return {
                "has_liquidity": True,
                "swept": swept,
                "liq_type": liq_type,
                "liq_level": lvl_r,
                "liq_line": {
                    "level": lvl_r,
                    "start_time": int(dates[idm_idx]),
                    "end_time": int(dates[-1]),
                    "type": label_str,
                    "color": "rgba(192, 132, 252, 0.92)",
                    "is_anchor": False,
                    "is_struct": True,
                    "is_choch": False,
                },
            }
    else:
        cand_highs = []
        for j in range(depart_idx + 2, scan_end):
            if (
                highs[j] >= highs[j - 1] and highs[j] >= highs[j - 2]
                and highs[j] >= highs[j + 1] and highs[j] >= highs[j + 2]
            ):
                subsequent_drop = float(highs[j]) - float(np.min(lows[j + 1 : min(total, j + 8)]))
                if subsequent_drop >= (0.45 * atr):
                    if poi_bot - (6.5 * atr) <= highs[j] <= poi_top - (0.05 * atr):
                        cand_highs.append(j)

        if cand_highs:
            idm_idx = cand_highs[0]
            idm_price = float(highs[idm_idx])
            eq_highs = [idx for idx in cand_highs if abs(highs[idx] - idm_price) <= 0.20 * atr]
            liq_type = "Range Liquidity (EQH)" if len(eq_highs) >= 2 else "Inducement (IDM)"
            lvl_r = round(idm_price, decimals)

            post_max = float(np.max(highs[idm_idx + 1:])) if idm_idx + 1 < total else idm_price
            swept = bool(post_max > idm_price + (0.01 * atr))
            status_tag = "SWEPT ✅" if swept else ""
            label_str = f"$$$ {liq_type} {status_tag} ({lvl_r:.{decimals}f})".replace("  ", " ")

            return {
                "has_liquidity": True,
                "swept": swept,
                "liq_type": liq_type,
                "liq_level": lvl_r,
                "liq_line": {
                    "level": lvl_r,
                    "start_time": int(dates[idm_idx]),
                    "end_time": int(dates[-1]),
                    "type": label_str,
                    "color": "rgba(192, 132, 252, 0.92)",
                    "is_anchor": False,
                    "is_struct": True,
                    "is_choch": False,
                },
            }

    return {"has_liquidity": False, "swept": False, "liq_type": "Inducement (IDM)", "liq_level": None, "liq_line": None}


def find_structural_targets(df: pd.DataFrame, raw_highs, raw_lows,
                            is_bullish: bool, poi_ref: float, current_price: float,
                            anchor_idx: int, atr: float, decimals: int) -> List[Dict]:
    """
    SINGLE SENSIBLE STRUCTURAL TARGET ENGINE (v11.2):
    - Targets the actual opposing swing high/low of the impulse leg (with a clean 1.25 * ATR floor
      and a realistic 4.2 * ATR cap so targets never project wildly into empty space).
    """
    highs = df['High'].values
    lows = df['Low'].values
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(highs)
    if total == 0:
        return []

    end_time = int(dates[-1])
    anchor_idx = int(np.clip(anchor_idx, 0, total - 1))
    anchor_time = int(dates[anchor_idx])

    min_dist_from_poi = 1.25 * atr
    max_sensible_dist = 4.20 * atr

    if is_bullish:
        leg_high = float(max(highs[anchor_idx:].max(), current_price))
        min_t1_floor = max(poi_ref + min_dist_from_poi, leg_high)
        max_t1_cap = max(leg_high + 0.35 * atr, poi_ref + max_sensible_dist)

        candidate_indices = set(int(i) for i in raw_highs if 0 <= int(i) < total)
        if anchor_idx < total:
            cycle_max_idx = anchor_idx + int(np.argmax(highs[anchor_idx:]))
            candidate_indices.add(cycle_max_idx)
        vis_start = max(0, total - 220)
        vis_max_idx = vis_start + int(np.argmax(highs[vis_start:]))
        candidate_indices.add(vis_max_idx)

        valid_candidates = []
        for idx in candidate_indices:
            if idx < 0 or idx >= total:
                continue
            lvl = float(highs[idx])
            if lvl < min_t1_floor - (0.15 * atr):
                continue
            if idx < total - 1 and np.max(highs[idx + 1:]) > lvl + (0.25 * atr):
                continue
            t_start = int(dates[idx]) if idx < total - 8 else anchor_time
            clamped_lvl = min(max(lvl, min_t1_floor), max_t1_cap)
            valid_candidates.append((clamped_lvl, t_start))

        valid_candidates.sort(key=lambda x: x[0])

        if valid_candidates:
            lvl, t_start = valid_candidates[0]
            lvl_r = round(lvl, decimals)
            return [{
                "target_num": 1,
                "level": lvl_r,
                "start_time": t_start,
                "end_time": end_time,
                "label": f"Target ({lvl_r:.{decimals}f})",
            }]

        proj_lvl = round(min(min_t1_floor, max_t1_cap), decimals)
        return [{
            "target_num": 1,
            "level": proj_lvl,
            "start_time": anchor_time,
            "end_time": end_time,
            "label": f"Target ({proj_lvl:.{decimals}f})",
        }]

    else:
        leg_low = float(min(lows[anchor_idx:].min(), current_price))
        max_t1_ceil = min(poi_ref - min_dist_from_poi, leg_low)
        min_t1_cap = min(leg_low - 0.35 * atr, poi_ref - max_sensible_dist)

        candidate_indices = set(int(i) for i in raw_lows if 0 <= int(i) < total)
        if anchor_idx < total:
            cycle_min_idx = anchor_idx + int(np.argmin(lows[anchor_idx:]))
            candidate_indices.add(cycle_min_idx)
        vis_start = max(0, total - 220)
        vis_min_idx = vis_start + int(np.argmin(lows[vis_start:]))
        candidate_indices.add(vis_min_idx)

        valid_candidates = []
        for idx in candidate_indices:
            if idx < 0 or idx >= total:
                continue
            lvl = float(lows[idx])
            if lvl > max_t1_ceil + (0.15 * atr):
                continue
            if idx < total - 1 and np.min(lows[idx + 1:]) < lvl - (0.25 * atr):
                continue
            t_start = int(dates[idx]) if idx < total - 8 else anchor_time
            clamped_lvl = max(min(lvl, max_t1_ceil), min_t1_cap)
            valid_candidates.append((clamped_lvl, t_start))

        valid_candidates.sort(key=lambda x: x[0], reverse=True)

        if valid_candidates:
            lvl, t_start = valid_candidates[0]
            lvl_r = round(lvl, decimals)
            return [{
                "target_num": 1,
                "level": lvl_r,
                "start_time": t_start,
                "end_time": end_time,
                "label": f"Target ({lvl_r:.{decimals}f})",
            }]

        proj_lvl = round(max(max_t1_ceil, min_t1_cap), decimals)
        return [{
            "target_num": 1,
            "level": proj_lvl,
            "start_time": anchor_time,
            "end_time": end_time,
            "label": f"Target ({proj_lvl:.{decimals}f})",
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

    atr = calculate_atr(df, 14)
    if atr == 0:
        atr = float(df['Close'].mean()) * 0.001

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

    macro_lookback = max(0, total - 160)
    macro_max_idx = macro_lookback + int(np.argmax(highs[macro_lookback:]))
    macro_min_idx = macro_lookback + int(np.argmin(lows[macro_lookback:]))
    macro_high_val = float(highs[macro_max_idx])
    macro_low_val = float(lows[macro_min_idx])
    macro_span = macro_high_val - macro_low_val

    if macro_span > (3.5 * atr):
        if macro_max_idx < macro_min_idx:
            inter_highs = [
                int(i) for i in raw_highs
                if macro_max_idx + 3 <= int(i) <= macro_min_idx - 3
                and (highs[int(i)] - macro_low_val) >= (2.0 * atr)
            ]
            major_ceiling = max([float(highs[i]) for i in inter_highs]) if inter_highs else (macro_low_val + 0.80 * macro_span)
            post_low_max_close = float(np.max(closes[macro_min_idx:]))
            if post_low_max_close <= major_ceiling + (0.10 * atr):
                is_bullish = False
                fresh_anchor_idx = int(macro_max_idx)
                fresh_anchor_time = int(dates[macro_max_idx])
                fresh_anchor_price = macro_high_val
        else:
            inter_lows = [
                int(i) for i in raw_lows
                if macro_min_idx + 3 <= int(i) <= macro_max_idx - 3
                and (macro_high_val - lows[int(i)]) >= (2.0 * atr)
            ]
            major_floor = min([float(lows[i]) for i in inter_lows]) if inter_lows else (macro_high_val - 0.80 * macro_span)
            post_high_min_close = float(np.min(closes[macro_max_idx:]))
            if post_high_min_close >= major_floor - (0.10 * atr):
                is_bullish = True
                fresh_anchor_idx = int(macro_min_idx)
                fresh_anchor_time = int(dates[macro_min_idx])
                fresh_anchor_price = macro_low_val

    if not is_bullish:
        if highs[macro_max_idx] > fresh_anchor_price and macro_max_idx < fresh_anchor_idx:
            if np.min(lows[fresh_anchor_idx:]) < np.min(lows[macro_max_idx:fresh_anchor_idx]):
                fresh_anchor_idx = int(macro_max_idx)
                fresh_anchor_time = int(dates[macro_max_idx])
                fresh_anchor_price = float(highs[macro_max_idx])
    else:
        if lows[macro_min_idx] < fresh_anchor_price and macro_min_idx < fresh_anchor_idx:
            if np.max(highs[fresh_anchor_idx:]) > np.max(highs[macro_min_idx:fresh_anchor_idx]):
                fresh_anchor_idx = int(macro_min_idx)
                fresh_anchor_time = int(dates[macro_min_idx])
                fresh_anchor_price = float(lows[macro_min_idx])

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
    poi_info = find_or_lock_unmitigated_poi(
        df, is_bullish, int(anchor_idx), anchor_time, struct_events, live_ref, atr, decimals, state_key=state_key
    )
    zone_boxes = [poi_info["box"]] if poi_info else []
    poi_level = poi_info["poi_level"] if poi_info else anchor_price

    liq_data = find_pullback_liquidity(df, is_bullish, poi_info, atr, decimals)
    if liq_data.get("liq_line"):
        all_lines.append(liq_data["liq_line"])

    targets = find_structural_targets(df, raw_highs, raw_lows, is_bullish, poi_level, live_ref, int(anchor_idx), atr, decimals)

    projection_path = []
    if poi_info is not None and len(targets) >= 1:
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

        if poi_info.get("status") == "UNTOUCHED":
            step_seconds = int(dates[-1] - dates[-2]) if len(dates) >= 2 else 3600
            projection_path = [
                {"time": int(dates[-1]), "price": round(live_ref, decimals), "label": "Now"},
                {"time": int(dates[-1] + step_seconds * 4), "price": round(poi_level, decimals), "label": "POI Entry Zone"},
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
        "peak_confirmed": peak_confirmed,
        "sweeps": [],
        "swing_order": swing_order,
        "raw_highs": raw_highs.tolist(),
        "raw_lows": raw_lows.tolist(),
        "trendline": None,
        "poi_info": poi_info,
        "liq_data": liq_data,
        "zone_boxes": zone_boxes,
        "poi_level": round(poi_level, decimals),
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
    break_kind = ms["break_kind"]
    break_level = ms["break_level"]
    poi_info = ms.get("poi_info")
    liq_data = ms.get("liq_data", {})
    zone_boxes = ms["zone_boxes"]
    targets = ms["targets"]

    opens = df['Open'].values
    closes = df['Close'].values
    highs = df['High'].values
    lows = df['Low'].values
    dates = df['Date'].values if 'Date' in df.columns else df.index.values
    total = len(closes)

    has_poi = poi_info is not None and len(zone_boxes) > 0
    poi_level = poi_info["poi_level"] if has_poi else anchor_price
    ob_range = poi_info["ob_range"] if has_poi else None
    fvg_range = poi_info["fvg_range"] if has_poi else None

    tp_level = targets[0]["level"] if len(targets) >= 1 else None

    has_liquidity = bool(liq_data.get("has_liquidity", False))
    liq_swept = bool(liq_data.get("swept", False))
    liq_type = liq_data.get("liq_type", "Inducement (IDM)")
    liq_level = liq_data.get("liq_level")

    dir_word = "BULLISH (BUY BIAS)" if is_bullish else "BEARISH (SELL BIAS)"
    peak_word = "Peak Low (W)" if is_bullish else "Peak High (M)"
    arrow = "▲" if is_bullish else "▼"
    min_be_buf = round(max(pip_size * 2.0, 0.04 * atr), decimals)

    if state_key and state_key not in CLOSED_SIGNALS:
        CLOSED_SIGNALS[state_key] = set()

    # 1. Manage existing locked trade in memory (65% TP / 1.25R Break-Even + Single TP Close)
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
            if state_key in POI_STATE:
                del POI_STATE[state_key]

    locked_trade = ACTIVE_TRADES.get(state_key) if state_key else None

    # 2. Scan post-departure candles (`depart_idx + 2` to `total - 1`) for 1H rejection inside `break_idx - 1` POI
    if locked_trade is None and has_poi and peak_confirmed:
        active_ob = zone_boxes[0]
        depart_idx = int(poi_info.get("depart_idx", anchor_idx + 2))
        if total > depart_idx + 2:
            search_start = max(depart_idx + 2, total - 35)
            for k in range(search_start, total):
                sig_ts = int(dates[k])
                if state_key and sig_ts in CLOSED_SIGNALS.get(state_key, set()):
                    continue

                c_open = float(opens[k])
                c_close = float(closes[k])
                c_high = float(highs[k])
                c_low = float(lows[k])
                p_high = float(highs[k - 1])
                p_low = float(lows[k - 1])

                body_sz = abs(c_close - c_open)
                u_wick = c_high - max(c_open, c_close)
                l_wick = min(c_open, c_close) - c_low

                if is_bullish:
                    touched_ob = (min(c_low, p_low) <= active_ob["top"] + 0.03 * atr) and (c_close >= active_ob["bottom"])
                    if not touched_ob:
                        continue

                    is_bull_rej = (
                        ((c_close > c_open) and (body_sz >= 0.08 * atr))
                        or (l_wick >= 0.14 * atr and c_close >= c_low + 0.50 * max(c_high - c_low, 1e-9))
                    )
                    not_chasing = (c_close - active_ob["top"]) <= (0.55 * atr)
                    if not (is_bull_rej and not_chasing):
                        continue

                    entry_k = round(c_close, decimals)
                    swing_floor = min(c_low, p_low)
                    ob_span = active_ob["top"] - active_ob["bottom"]
                    if ob_span > (1.20 * atr):
                        floor_k = max(active_ob["bottom"], swing_floor - 0.10 * atr)
                    else:
                        floor_k = min(active_ob["bottom"], swing_floor)

                    init_sl_k = round(min(floor_k - 0.22 * atr, entry_k - 0.35 * atr), decimals)
                    risk_k = max(abs(entry_k - init_sl_k), 1e-6)

                    t_list = find_structural_targets(df.iloc[:k + 1], ms["raw_highs"], ms["raw_lows"], True, entry_k, entry_k, anchor_idx, atr, decimals)
                    if not t_list:
                        continue
                    tp_k = t_list[0]["level"]
                    tp_dist_k = abs(tp_k - entry_k)
                    rr_k = round(tp_dist_k / risk_k, 2)
                    if rr_k < MIN_ALLOWED_RR:
                        continue

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

                    poi_snap = dict(poi_info)
                    poi_snap["box"] = dict(active_ob)
                    poi_snap["box"]["label"] = f"ACTIVE BUY POI ({active_ob['bottom']:.{decimals}f}–{active_ob['top']:.{decimals}f})"

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
                        "risk_reward": rr_k,
                        "ob_range": ob_range,
                        "fvg_range": fvg_range,
                        "poi_snapshot": poi_snap,
                    }
                    if state_key:
                        ACTIVE_TRADES[state_key] = locked_trade
                    break

                else:
                    touched_ob = (max(c_high, p_high) >= active_ob["bottom"] - 0.03 * atr) and (c_close <= active_ob["top"])
                    if not touched_ob:
                        continue

                    is_bear_rej = (
                        ((c_close < c_open) and (body_sz >= 0.08 * atr))
                        or (u_wick >= 0.14 * atr and c_close <= c_high - 0.50 * max(c_high - c_low, 1e-9))
                    )
                    not_chasing = (active_ob["bottom"] - c_close) <= (0.55 * atr)
                    if not (is_bear_rej and not_chasing):
                        continue

                    entry_k = round(c_close, decimals)
                    swing_ceil = max(c_high, p_high)
                    ob_span = active_ob["top"] - active_ob["bottom"]
                    if ob_span > (1.20 * atr):
                        ceil_k = min(active_ob["top"], swing_ceil + 0.10 * atr)
                    else:
                        ceil_k = max(active_ob["top"], swing_ceil)

                    init_sl_k = round(max(ceil_k + 0.22 * atr, entry_k + 0.35 * atr), decimals)
                    risk_k = max(abs(init_sl_k - entry_k), 1e-6)

                    t_list = find_structural_targets(df.iloc[:k + 1], ms["raw_highs"], ms["raw_lows"], False, entry_k, entry_k, anchor_idx, atr, decimals)
                    if not t_list:
                        continue
                    tp_k = t_list[0]["level"]
                    tp_dist_k = abs(entry_k - tp_k)
                    rr_k = round(tp_dist_k / risk_k, 2)
                    if rr_k < MIN_ALLOWED_RR:
                        continue

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

                    poi_snap = dict(poi_info)
                    poi_snap["box"] = dict(active_ob)
                    poi_snap["box"]["label"] = f"ACTIVE SELL POI ({active_ob['bottom']:.{decimals}f}–{active_ob['top']:.{decimals}f})"

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
                        "risk_reward": rr_k,
                        "ob_range": ob_range,
                        "fvg_range": fvg_range,
                        "poi_snapshot": poi_snap,
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
        f_rr = locked_trade["risk_reward"]
        f_dir = locked_trade["signal"]
        l_ob = locked_trade.get("ob_range") or f"{f_entry:.{decimals}f}"
        l_fvg = locked_trade.get("fvg_range") or "Confirmed"

        if locked_trade.get("poi_snapshot") and locked_trade["poi_snapshot"].get("box"):
            active_box = dict(locked_trade["poi_snapshot"]["box"])
            active_box["end_time"] = int(full_df['Date'].iloc[-1]) if 'Date' in full_df.columns else int(dates[-1])
            ms["zone_boxes"] = [active_box]

        step_seconds = int(dates[-1] - dates[-2]) if len(dates) >= 2 else 3600
        now_ts = int(full_df['Date'].iloc[-1]) if 'Date' in full_df.columns else int(dates[-1])
        live_p = round(current_price if current_price > 0 else float(closes[-1]), decimals)
        mid_p = round((live_p + f_tp) / 2.0, decimals)
        ms["projection_path"] = [
            {"time": now_ts, "price": live_p, "label": "Now"},
            {"time": now_ts + step_seconds * 4, "price": mid_p, "label": "Path"},
            {"time": now_ts + step_seconds * 8, "price": f_tp, "label": "TP"},
        ]

        sl_status_txt = f"SL @ BE ({f_sl:.{decimals}f}) 🛡️" if f_be else f"Entered @ {f_entry:.{decimals}f} ✅"
        checklist = [
            {"step": f"1. Market Structure ({peak_word} & {break_kind} {arrow})", "done": True, "detail": f"Cleared {break_level:.{decimals}f} ✅"},
            {"step": "2. Point of Interest (Origin OB)", "done": True, "detail": f"{l_ob} ✅"},
            {"step": "3. Fair Value Gap (FVG)", "done": True, "detail": f"{l_fvg} ✅"},
            {"step": f"4. Liquidity & 1H Close (RR 1:{f_rr})", "done": True, "detail": sl_status_txt},
        ]

        active_conf = 84 + (4 if has_liquidity else 0) + (4 if htf_aligned else 0) + (2 if f_be else 0)
        sl_label = f"SL (BE 🛡️): {f_sl:.{decimals}f}" if f_be else f"SL: {f_sl:.{decimals}f}"
        trigger_note = (
            f"{'🟢 BUY' if is_bullish else '🔴 SELL'} EXECUTING: Locked Entry @ {f_entry:.{decimals}f} | "
            f"TP: {f_tp:.{decimals}f} | {sl_label}"
        )

        return {
            "signal": f_dir,
            "confidence": min(96, active_conf),
            "setup_name": f"SMC A+ {f_dir} Active" + (" (Risk-Free BE 🛡️)" if f_be else ""),
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

    if not has_poi:
        checklist = [
            {"step": f"1. Market Structure ({peak_word} & {break_kind} {arrow})", "done": bool(peak_confirmed), "detail": f"Cleared {break_level:.{decimals}f} ✅"},
            {"step": "2. Point of Interest (Origin OB)", "done": False, "detail": "Waiting for Unmitigated OB"},
            {"step": "3. Fair Value Gap (FVG)", "done": False, "detail": "Waiting for BOS Impulse"},
            {"step": "4. Liquidity & 1H Confirmation", "done": False, "detail": "Wait for POI First"},
        ]
        note = (
            f"⏳ NO UNMITIGATED POI YET: {dir_word} confirmed via {break_kind} {arrow} ({break_level:.{decimals}f}), "
            f"but prior Order Blocks have already been mitigated. Waiting for a new BOS."
        )
        return {
            "signal": "WAIT",
            "confidence": 48,
            "setup_name": "Waiting for Unmitigated POI",
            "checklist": checklist,
            "execution_plan": {
                "stage": "AWAITING_PULLBACK",
                "trigger_condition": note,
                "planned_entry": None,
                "planned_sl": None,
                "planned_tp1": None,
                "planned_tp2": None,
                "risk_reward": "—",
            },
            "entry_note": note,
            "trade_levels": None,
        }

    active_ob = zone_boxes[0]
    poi_status = poi_info.get("status", "UNTOUCHED")
    has_fvg_flag = bool(poi_info.get("has_fvg", False))
    live_open = float(full_df['Open'].iloc[-1])

    if is_bullish:
        in_zone_now = (poi_status == "TAPPED_ACTIVE")
        ob_span = active_ob["top"] - active_ob["bottom"]
        eff_floor = (active_ob["top"] - 0.85 * atr) if ob_span > (1.20 * atr) else active_ob["bottom"]
        planned_sl = round(min(eff_floor, poi_level) - (0.22 * atr), decimals)
        risk = max(abs(poi_level - planned_sl), 1e-6)
        best_rr = round(abs(tp_level - poi_level) / risk, 2) if tp_level else 2.5

        step4_detail = (
            "Watch 1H Close!" if in_zone_now
            else f"{liq_type} {'SWEPT ✅' if liq_swept else f'@ {liq_level:.{decimals}f}'}" if has_liquidity
            else f"Wait for {poi_level:.{decimals}f} (1:{best_rr})"
        )

        checklist = [
            {"step": f"1. Market Structure ({peak_word} & {break_kind} {arrow})", "done": bool(peak_confirmed), "detail": f"Cleared {break_level:.{decimals}f} ✅"},
            {"step": "2. Point of Interest (Origin OB)", "done": True, "detail": f"{ob_range} {'🎯' if in_zone_now else '🔒'}"},
            {"step": "3. Fair Value Gap (FVG)", "done": True, "detail": f"{fvg_range} {'✅' if has_fvg_flag else '⚡'}"},
            {"step": f"4. Liquidity & 1H Close (RR 1:{best_rr})", "done": False, "detail": step4_detail},
        ]

        if in_zone_now:
            stage = "IN_ZONE_CONFIRMING"
            trig_lvl = round(max(live_open, active_ob["bottom"]), decimals)
            note = f"🎯 PRICE TAPPED BUY POI ({ob_range})! Wait for 1H candle to close GREEN above {trig_lvl:.{decimals}f} to lock BUY."
        else:
            stage = "AWAITING_PULLBACK"
            pips_away = round(abs(current_price - poi_level) / max(pip_size, 1e-6), 1)
            liq_hint = (
                f" {liq_type} ({liq_level:.{decimals}f}) SWEPT ✅ — heading to Buy POI!"
                if (has_liquidity and liq_swept)
                else f" {liq_type} sitting @ {liq_level:.{decimals}f} above POI."
                if has_liquidity else ""
            )
            note = f"🔒 UNMITIGATED BUY POI ({ob_range}) | FVG ({fvg_range}): Price is {pips_away} pips above Buy POI.{liq_hint}"

        return {
            "signal": "WAIT",
            "confidence": 68 if in_zone_now else (62 if (has_liquidity and liq_swept) else 56),
            "setup_name": "SMC Bullish A+ — " + ("In Buy POI" if in_zone_now else "Locked Buy POI"),
            "checklist": checklist,
            "execution_plan": {
                "stage": stage,
                "trigger_condition": note,
                "planned_entry": round(poi_level, decimals),
                "planned_sl": planned_sl,
                "planned_tp1": round(tp_level, decimals) if tp_level else None,
                "planned_tp2": None,
                "risk_reward": best_rr,
            },
            "entry_note": note,
            "trade_levels": None,
        }

    else:
        in_zone_now = (poi_status == "TAPPED_ACTIVE")
        ob_span = active_ob["top"] - active_ob["bottom"]
        eff_ceil = (active_ob["bottom"] + 0.85 * atr) if ob_span > (1.20 * atr) else active_ob["top"]
        planned_sl = round(max(eff_ceil, poi_level) + (0.22 * atr), decimals)
        risk = max(abs(planned_sl - poi_level), 1e-6)
        best_rr = round(abs(poi_level - tp_level) / risk, 2) if tp_level else 2.5

        step4_detail = (
            "Watch 1H Close!" if in_zone_now
            else f"{liq_type} {'SWEPT ✅' if liq_swept else f'@ {liq_level:.{decimals}f}'}" if has_liquidity
            else f"Wait for {poi_level:.{decimals}f} (1:{best_rr})"
        )

        checklist = [
            {"step": f"1. Market Structure ({peak_word} & {break_kind} {arrow})", "done": bool(peak_confirmed), "detail": f"Cleared {break_level:.{decimals}f} ✅"},
            {"step": "2. Point of Interest (Origin OB)", "done": True, "detail": f"{ob_range} {'🎯' if in_zone_now else '🔒'}"},
            {"step": "3. Fair Value Gap (FVG)", "done": True, "detail": f"{fvg_range} {'✅' if has_fvg_flag else '⚡'}"},
            {"step": f"4. Liquidity & 1H Close (RR 1:{best_rr})", "done": False, "detail": step4_detail},
        ]

        if in_zone_now:
            stage = "IN_ZONE_CONFIRMING"
            trig_lvl = round(min(live_open, active_ob["top"]), decimals)
            note = f"🎯 PRICE TAPPED SELL POI ({ob_range})! Wait for 1H candle to close RED below {trig_lvl:.{decimals}f} to lock SELL."
        else:
            stage = "AWAITING_PULLBACK"
            pips_away = round(abs(poi_level - current_price) / max(pip_size, 1e-6), 1)
            liq_hint = (
                f" {liq_type} ({liq_level:.{decimals}f}) SWEPT ✅ — rallying toward Sell POI!"
                if (has_liquidity and liq_swept)
                else f" {liq_type} sitting @ {liq_level:.{decimals}f} below POI."
                if has_liquidity else ""
            )
            note = f"🔒 UNMITIGATED SELL POI ({ob_range}) | FVG ({fvg_range}): Price is {pips_away} pips below Sell POI.{liq_hint}"

        return {
            "signal": "WAIT",
            "confidence": 68 if in_zone_now else (62 if (has_liquidity and liq_swept) else 56),
            "setup_name": "SMC Bearish A+ — " + ("In Sell POI" if in_zone_now else "Locked Sell POI"),
            "checklist": checklist,
            "execution_plan": {
                "stage": stage,
                "trigger_condition": note,
                "planned_entry": round(poi_level, decimals),
                "planned_sl": planned_sl,
                "planned_tp1": round(tp_level, decimals) if tp_level else None,
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

        zone_boxes = ms.get('zone_boxes', [])

        checklist = entry_eval.get("checklist", [])
        reasoning = [
            f"{'✅' if c['done'] else '⏳'} {c['step']} ({c['detail']})"
            for c in checklist
        ]
        reasoning.append(entry_eval["entry_note"])

        has_valid_poi = len(zone_boxes) > 0 or (entry_eval["trade_levels"] is not None)
        t1_out = (
            entry_eval["trade_levels"]["target_1"] if entry_eval["trade_levels"]
            else (targets[0]["level"] if (has_valid_poi and len(targets) >= 1) else None)
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
                "poi": ms["poi_level"] if has_valid_poi else None,
                "target1": t1_out,
                "target2": None,
                "ema200": round(ema_200, decimals),
                "ema50": round(ema_50, decimals),
            },
            "visuals": {
                "bos_lines": lines,
                "sweeps": [],
                "trendline": None,
                "order_blocks": zone_boxes,
                "targets": targets if has_valid_poi else [],
                "projection_path": ms["projection_path"],
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
            "✅ ENGINE VERSION": "AuraBrain v11.2 — Single Sensible Target (TP) + Anti-Duplicate Closed Signal Guard + 65% TP Break-Even Engine",
            "📊 INSTRUMENT": req.currency,
            "🎯 STRUCTURE": ms['cycle'],
            "⚓ PEAK ANCHOR": ms['anchor'],
            "📏 LINES": ms['lines'],
            "🔒 LOCKED POI": ms.get('poi_info'),
            "💧 LIQUIDITY": ms.get('liq_data'),
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