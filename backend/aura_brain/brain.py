import os
import pandas as pd
import numpy as np
import math
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import logging
from contextlib import asynccontextmanager
import httpx
from starlette.responses import Response

# ─────────────────────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────────────────────
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

CSV_FILENAME = "1h.csv"
NODE_URL = "http://127.0.0.1:10000"

DROP_FORMING_CANDLE = True

# ─────────────────────────────────────────────────────────────────────────────
# SMC 4-CONFLUENCE A+ STRATEGY TUNING (BOS/CHoCH + POI OB + FVG + LIQUIDITY)
# ─────────────────────────────────────────────────────────────────────────────
BASE_SWING_ORDER             = 4
FLIP_BUFFER_ATR              = 0.20
FLIP_CONFIRM_BARS            = 1
ANCHOR_MIN_MOVE_ATR          = 1.4
SWING_PROMINENCE_ATR         = 0.85
MIN_FVG_SIZE_ATR             = 0.08
MIN_ALLOWED_RR               = 1.8

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuraBrain")

MARKET_MEMORY = {"df": None}

CYCLE_STATE: Dict[str, Dict]   = {}
ORDER_CACHE: Dict[str, int]    = {}
ACTIVE_TRADES: Dict[str, Dict] = {}

base_dir = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# CORE ENGINE — 4-Confluence SMC Strategy (BOS/CHoCH + Origin POI + FVG + IDM)
# ─────────────────────────────────────────────────────────────────────────────

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
    high  = df['High'].values
    low   = df['Low'].values
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
    order = int(np.clip(round(6.0 - (efficiency * 3.0)), 3, 6))
    return order


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
            left_prom  = highs[idx] - np.min(lows[idx - left_w : idx])
            right_prom = highs[idx] - np.min(lows[idx + 1 : idx + right_w + 1])
            if max(left_prom, right_prom) >= min_prominence and min(left_prom, right_prom) >= min_prominence * 0.45:
                if not filtered_highs or (idx - filtered_highs[-1]) >= right_w:
                    filtered_highs.append(idx)
                elif highs[idx] > highs[filtered_highs[-1]]:
                    filtered_highs[-1] = idx

        l_lows = lows[idx - left_w : idx]
        r_lows = lows[idx + 1 : idx + right_w + 1]
        if lows[idx] <= np.min(l_lows) and lows[idx] < np.min(r_lows):
            left_prom  = np.max(highs[idx - left_w : idx]) - lows[idx]
            right_prom = np.max(highs[idx + 1 : idx + right_w + 1]) - lows[idx]
            if max(left_prom, right_prom) >= min_prominence and min(left_prom, right_prom) >= min_prominence * 0.45:
                if not filtered_lows or (idx - filtered_lows[-1]) >= right_w:
                    filtered_lows.append(idx)
                elif lows[idx] < lows[filtered_lows[-1]]:
                    filtered_lows[-1] = idx

    return np.array(filtered_highs, dtype=int), np.array(filtered_lows, dtype=int)


def find_structural_targets(df: pd.DataFrame, raw_highs, raw_lows,
                            is_bullish: bool, ref_price: float, anchor_idx: int,
                            anchor_price: float, atr: float, decimals: int) -> List[Dict]:
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(highs)
    if total == 0:
        return []

    end_time   = int(dates[-1])
    anchor_idx = int(np.clip(anchor_idx, 0, total - 1))

    min_dist       = 0.90 * atr
    min_separation = 1.00 * atr
    targets = []

    if is_bullish:
        candidate_indices = set(int(i) for i in raw_highs if 0 <= int(i) < total)
        if anchor_idx < total - 1:
            cycle_max_idx = anchor_idx + int(np.argmax(highs[anchor_idx:]))
            candidate_indices.add(cycle_max_idx)
        vis_start = max(0, total - 180)
        vis_max_idx = vis_start + int(np.argmax(highs[vis_start:]))
        candidate_indices.add(vis_max_idx)

        valid_candidates = []
        for idx in candidate_indices:
            if idx < 0 or idx >= total:
                continue
            lvl = float(highs[idx])
            if lvl < ref_price + min_dist:
                continue
            if idx < total - 1 and np.max(highs[idx + 1:]) > lvl + (0.25 * atr):
                continue
            valid_candidates.append((lvl, int(dates[idx])))

        valid_candidates.sort(key=lambda x: x[0])

        for lvl, t_start in valid_candidates:
            if not targets or (lvl - targets[-1]["level"]) >= min_separation:
                t_num = len(targets) + 1
                targets.append({
                    "target_num": t_num,
                    "level":      round(lvl, decimals),
                    "start_time": t_start,
                    "end_time":   end_time,
                    "label":      f"Target {t_num} ({lvl:.{decimals}f})",
                })
                if len(targets) >= 2:
                    break

        while len(targets) < 2:
            t_num = len(targets) + 1
            base_lvl = targets[-1]["level"] if targets else max(ref_price, float(highs[anchor_idx:].max()))
            proj_lvl = round(base_lvl + (1.6 * atr), decimals)
            if proj_lvl < ref_price + min_dist:
                proj_lvl = round(ref_price + (1.6 * atr * t_num), decimals)
            targets.append({
                "target_num": t_num,
                "level":      proj_lvl,
                "start_time": int(dates[anchor_idx]),
                "end_time":   end_time,
                "label":      f"Target {t_num} ({proj_lvl:.{decimals}f})",
            })

    else:
        candidate_indices = set(int(i) for i in raw_lows if 0 <= int(i) < total)
        if anchor_idx < total - 1:
            cycle_min_idx = anchor_idx + int(np.argmin(lows[anchor_idx:]))
            candidate_indices.add(cycle_min_idx)
        vis_start = max(0, total - 180)
        vis_min_idx = vis_start + int(np.argmin(lows[vis_start:]))
        candidate_indices.add(vis_min_idx)

        valid_candidates = []
        for idx in candidate_indices:
            if idx < 0 or idx >= total:
                continue
            lvl = float(lows[idx])
            if lvl > ref_price - min_dist:
                continue
            if idx < total - 1 and np.min(lows[idx + 1:]) < lvl - (0.25 * atr):
                continue
            valid_candidates.append((lvl, int(dates[idx])))

        valid_candidates.sort(key=lambda x: x[0], reverse=True)

        for lvl, t_start in valid_candidates:
            if not targets or (targets[-1]["level"] - lvl) >= min_separation:
                t_num = len(targets) + 1
                targets.append({
                    "target_num": t_num,
                    "level":      round(lvl, decimals),
                    "start_time": t_start,
                    "end_time":   end_time,
                    "label":      f"Target {t_num} ({lvl:.{decimals}f})",
                })
                if len(targets) >= 2:
                    break

        while len(targets) < 2:
            t_num = len(targets) + 1
            base_lvl = targets[-1]["level"] if targets else min(ref_price, float(lows[anchor_idx:].min()))
            proj_lvl = round(base_lvl - (1.6 * atr), decimals)
            if proj_lvl > ref_price - min_dist:
                proj_lvl = round(ref_price - (1.6 * atr * t_num), decimals)
            targets.append({
                "target_num": t_num,
                "level":      proj_lvl,
                "start_time": int(dates[anchor_idx]),
                "end_time":   end_time,
                "label":      f"Target {t_num} ({proj_lvl:.{decimals}f})",
            })

    return targets


def find_origin_poi_with_fvg(df: pd.DataFrame, is_bullish: bool, origin_start_idx: int,
                             break_idx: int, atr: float, decimals: int) -> Dict:
    """
    CONFLUENCE #2 & #3 (Origin Order Block POI + Mandatory 3-Candle Fair Value Gap):
    Walks the Origin range right before the BOS/CHoCH impulse to locate the
    LAST opposite/reaction candle before the break that left a 3-candle FVG.
    """
    opens  = df['Open'].values
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(closes)

    origin_start_idx = int(np.clip(origin_start_idx, 0, max(0, total - 4)))
    break_idx        = int(np.clip(break_idx, origin_start_idx + 1, max(origin_start_idx + 1, total - 2)))

    best_poi = None

    search_lo = max(0, origin_start_idx - 3)
    search_hi = min(total - 3, break_idx + 2)

    if is_bullish:
        # Walk backwards from the break impulse to find the last reaction candle with a Bullish FVG
        for k in range(search_hi, search_lo - 1, -1):
            fvg_bot = float(highs[k])
            fvg_top = float(lows[k + 2])
            fvg_sz  = fvg_top - fvg_bot
            is_disp = (closes[k + 1] > opens[k + 1]) and ((closes[k + 1] - opens[k + 1]) >= 0.25 * atr)

            if fvg_sz >= (MIN_FVG_SIZE_ATR * atr) and is_disp:
                # Refinement: pick the reaction candle (or lowest candle at k / k-1) right before the impulse
                ob_idx = k
                if k > search_lo and lows[k - 1] < lows[k] and closes[k - 1] <= opens[k - 1]:
                    ob_idx = k - 1

                ob_bot  = float(min(lows[ob_idx], lows[k]))
                ob_top  = float(max(highs[ob_idx], opens[ob_idx], closes[ob_idx]))
                if (ob_top - ob_bot) > 1.15 * atr:
                    ob_top = ob_bot + 0.55 * atr
                if (ob_top - ob_bot) < 0.18 * atr:
                    ob_top = ob_bot + 0.28 * atr

                best_poi = {
                    "poi_idx":    int(ob_idx),
                    "poi_time":   int(dates[ob_idx]),
                    "poi_top":    round(ob_top, decimals),
                    "poi_bottom": round(ob_bot, decimals),
                    "has_fvg":    True,
                    "fvg_time":   int(dates[k + 1]),
                    "fvg_top":    round(fvg_top, decimals),
                    "fvg_bottom": round(fvg_bot, decimals),
                    "fvg_size":   round(fvg_sz, decimals),
                }
                break

        if best_poi is None:
            # Fallback to the lowest candle in the origin range if no strict 3-bar gap remained open
             local_min_idx = search_lo + int(np.argmin(lows[search_lo : search_hi + 1]))
            ob_bot = float(lows[local_min_idx])
            ob_top = float(min(highs[local_min_idx], ob_bot + 0.45 * atr))
            if (ob_top - ob_bot) < 0.20 * atr:
                ob_top = ob_bot + 0.32 * atr

            # Check if any 3-candle micro-imbalance exists near local_min_idx
            has_micro_fvg = False
            fvg_bot, fvg_top, fvg_t = ob_top, ob_top + 0.15 * atr, int(dates[local_min_idx])
            for m in range(local_min_idx, min(total - 2, local_min_idx + 5)):
                if lows[m + 2] > highs[m]:
                    has_micro_fvg = True
                    fvg_bot = float(highs[m])
                    fvg_top = float(lows[m + 2])
                    fvg_t   = int(dates[m + 1])
                    break

            best_poi = {
                "poi_idx":    int(local_min_idx),
                "poi_time":   int(dates[local_min_idx]),
                "poi_top":    round(ob_top, decimals),
                "poi_bottom": round(ob_bot, decimals),
                "has_fvg":    has_micro_fvg,
                "fvg_time":   fvg_t,
                "fvg_top":    round(fvg_top, decimals),
                "fvg_bottom": round(fvg_bot, decimals),
                "fvg_size":   round(max(0.0, fvg_top - fvg_bot), decimals),
            }

    else:
        for k in range(search_hi, search_lo - 1, -1):
            fvg_top = float(lows[k])
            fvg_bot = float(highs[k + 2])
            fvg_sz  = fvg_top - fvg_bot
            is_disp = (closes[k + 1] < opens[k + 1]) and ((opens[k + 1] - closes[k + 1]) >= 0.25 * atr)

            if fvg_sz >= (MIN_FVG_SIZE_ATR * atr) and is_disp:
                ob_idx = k
                if k > search_lo and highs[k - 1] > highs[k] and closes[k - 1] >= opens[k - 1]:
                    ob_idx = k - 1

                ob_top  = float(max(highs[ob_idx], highs[k]))
                ob_bot  = float(min(lows[ob_idx], opens[ob_idx], closes[ob_idx]))
                if (ob_top - ob_bot) > 1.15 * atr:
                    ob_bot = ob_top - 0.55 * atr
                if (ob_top - ob_bot) < 0.18 * atr:
                    ob_bot = ob_top - 0.28 * atr

                best_poi = {
                    "poi_idx":    int(ob_idx),
                    "poi_time":   int(dates[ob_idx]),
                    "poi_top":    round(ob_top, decimals),
                    "poi_bottom": round(ob_bot, decimals),
                    "has_fvg":    True,
                    "fvg_time":   int(dates[k + 1]),
                    "fvg_top":    round(fvg_top, decimals),
                    "fvg_bottom": round(fvg_bot, decimals),
                    "fvg_size":   round(fvg_sz, decimals),
                }
                break

        if best_poi is None:
            local_max_idx = search_lo + int(np.argmax(highs[search_lo : search_hi + 1]))
            ob_top = float(highs[local_max_idx])
            ob_bot = float(max(lows[local_max_idx], ob_top - 0.45 * atr))
            if (ob_top - ob_bot) < 0.20 * atr:
                ob_bot = ob_top - 0.32 * atr

            has_micro_fvg = False
            fvg_top, fvg_bot, fvg_t = ob_bot, ob_bot - 0.15 * atr, int(dates[local_max_idx])
            for m in range(local_max_idx, min(total - 2, local_max_idx + 5)):
                if highs[m + 2] < lows[m]:
                    has_micro_fvg = True
                    fvg_top = float(lows[m])
                    fvg_bot = float(highs[m + 2])
                    fvg_t   = int(dates[m + 1])
                    break

            best_poi = {
                "poi_idx":    int(local_max_idx),
                "poi_time":   int(dates[local_max_idx]),
                "poi_top":    round(ob_top, decimals),
                "poi_bottom": round(ob_bot, decimals),
                "has_fvg":    has_micro_fvg,
                "fvg_time":   fvg_t,
                "fvg_top":    round(fvg_top, decimals),
                "fvg_bottom": round(fvg_bot, decimals),
                "fvg_size":   round(max(0.0, fvg_top - fvg_bot), decimals),
            }

    return best_poi


def find_liquidity_before_poi(df: pd.DataFrame, is_bullish: bool, poi_idx: int,
                              poi_top: float, poi_bottom: float,
                              raw_highs: np.ndarray, raw_lows: np.ndarray,
                              atr: float, decimals: int) -> Dict:
    """
    CONFLUENCE #4 (Liquidity / Inducement Built & Swept Before POI):
    Identifies Inducement (IDM), Equal Highs/Lows (Range Liquidity), or
    internal pullback lows/highs sitting directly in front of the POI.
    """
    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(closes)

    if poi_idx >= total - 3:
        return {"has_liquidity": False, "swept": False, "liq_level": None, "liq_box": None, "liq_type": "None"}

    if is_bullish:
        # Look for an internal swing low / inducement low formed after the impulse above the Buy POI
        cand_lows = [
            int(i) for i in raw_lows
            if poi_idx + 2 < int(i) < total
            and poi_top < lows[int(i)] <= poi_top + (3.2 * atr)
        ]
        if not cand_lows:
            # Check for local pullback lows / range equal lows above POI
            for j in range(poi_idx + 3, total - 1):
                if lows[j] <= lows[j - 1] and lows[j] <= lows[j + 1]:
                    if poi_top < lows[j] <= poi_top + (2.8 * atr):
                        cand_lows.append(j)

        if cand_lows:
            idm_idx   = cand_lows[0]
            idm_price = float(lows[idm_idx])
            # Detect if Equal Lows (Range Liquidity) or single Inducement Low
            eq_lows   = [idx for idx in cand_lows if abs(lows[idx] - idm_price) <= 0.25 * atr]
            liq_type  = "Range Liquidity (EQL)" if len(eq_lows) >= 2 else "Inducement (IDM)"

            # Was this liquidity swept by a subsequent candle dipping below it into/toward the POI?
            post_min = float(np.min(lows[idm_idx + 1:])) if idm_idx + 1 < total else idm_price
            swept    = bool(post_min < idm_price - (0.03 * atr))

            band = 0.14 * atr
            return {
                "has_liquidity": True,
                "swept":         swept,
                "liq_type":      liq_type,
                "liq_level":     round(idm_price, decimals),
                "liq_idx":       int(idm_idx),
                "liq_box": {
                    "type":      "LIQ_ZONE",
                    "top":       round(idm_price + band, decimals),
                    "bottom":    round(idm_price - band, decimals),
                    "time":      int(dates[idm_idx]),
                    "end_time":  int(dates[-1]),
                    "label":     f"$$$ {liq_type.upper()} ({idm_price:.{decimals}f})" + (" [SWEPT ✅]" if swept else ""),
                    "mitigated": swept,
                }
            }

    else:
        cand_highs = [
            int(i) for i in raw_highs
            if poi_idx + 2 < int(i) < total
            and poi_bottom - (3.2 * atr) <= highs[int(i)] < poi_bottom
        ]
        if not cand_highs:
            for j in range(poi_idx + 3, total - 1):
                if highs[j] >= highs[j - 1] and highs[j] >= highs[j + 1]:
                    if poi_bottom - (2.8 * atr) <= highs[j] < poi_bottom:
                        cand_highs.append(j)

        if cand_highs:
            idm_idx   = cand_highs[0]
            idm_price = float(highs[idm_idx])
            eq_highs  = [idx for idx in cand_highs if abs(highs[idx] - idm_price) <= 0.25 * atr]
            liq_type  = "Range Liquidity (EQH)" if len(eq_highs) >= 2 else "Inducement (IDM)"

            post_max = float(np.max(highs[idm_idx + 1:])) if idm_idx + 1 < total else idm_price
            swept    = bool(post_max > idm_price + (0.03 * atr))

            band = 0.14 * atr
            return {
                "has_liquidity": True,
                "swept":         swept,
                "liq_type":      liq_type,
                "liq_level":     round(idm_price, decimals),
                "liq_idx":       int(idm_idx),
                "liq_box": {
                    "type":      "LIQ_ZONE",
                    "top":       round(idm_price + band, decimals),
                    "bottom":    round(idm_price - band, decimals),
                    "time":      int(dates[idm_idx]),
                    "end_time":  int(dates[-1]),
                    "label":     f"$$$ {liq_type.upper()} ({idm_price:.{decimals}f})" + (" [SWEPT ✅]" if swept else ""),
                    "mitigated": swept,
                }
            }

    return {"has_liquidity": False, "swept": False, "liq_level": None, "liq_box": None, "liq_type": "Building..."}


def detect_liquidity_sweeps(df: pd.DataFrame, swing_highs: np.ndarray, swing_lows: np.ndarray, atr: float) -> List[Dict]:
    sweeps = []
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    min_wick = atr * 0.20
    total    = len(closes)
    cutoff   = max(0, total - 200)

    recent_highs = [int(i) for i in swing_highs if cutoff <= int(i) < total]
    recent_lows  = [int(i) for i in swing_lows if cutoff <= int(i) < total]

    for sh_idx in recent_highs:
        sh_price = highs[sh_idx]
        for i in range(sh_idx + 1, min(sh_idx + 80, total)):
            wick_above = highs[i] - sh_price
            if closes[i] > sh_price + (atr * 0.25):
                break
            if wick_above >= min_wick and closes[i] < sh_price:
                sweeps.append({
                    "type":      "BEAR_SWEEP",
                    "level":     float(sh_price),
                    "sweep_idx": int(i),
                    "time":      int(dates[i]) if len(dates) > i else 0,
                    "wick_size": float(wick_above),
                })
                break

    for sl_idx in recent_lows:
        sl_price = lows[sl_idx]
        for i in range(sl_idx + 1, min(sl_idx + 80, total)):
            wick_below = sl_price - lows[i]
            if closes[i] < sl_price - (atr * 0.25):
                break
            if wick_below >= min_wick and closes[i] > sl_price:
                sweeps.append({
                    "type":      "BULL_SWEEP",
                    "level":     float(sl_price),
                    "sweep_idx": int(i),
                    "time":      int(dates[i]) if len(dates) > i else 0,
                    "wick_size": float(wick_below),
                })
                break
    sweeps.sort(key=lambda x: x['sweep_idx'])
    return sweeps[-5:]


def analyze_market_structure(df: pd.DataFrame, profile: Dict,
                             swing_order_override: Optional[int] = None,
                             state_key: Optional[str] = None,
                             drop_forming: bool = False) -> Dict:
    if drop_forming and len(df) > 60:
        df = df.iloc[:-1].reset_index(drop=True)

    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(closes)
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

    ema_50_array  = df['Close'].ewm(span=50, adjust=False).mean().values
    ema_200_array = df['Close'].ewm(span=200, adjust=False).mean().values

    # ── Track BOS (Break of Structure) & CHoCH (Change of Character) Events ──
    highs_set = set(raw_highs.tolist())
    lows_set  = set(raw_lows.tolist())

    seed_idx = min(50, total - 1)
    is_bull  = bool(ema_50_array[seed_idx] > ema_200_array[seed_idx])

    active_sh_idx  = 0
    active_sl_idx  = 0
    broken_sh_set  = set()
    broken_sl_set  = set()
    buf            = FLIP_BUFFER_ATR * atr

    structure_breaks = []

    for i in range(total):
        confirmed_bar = i - swing_order
        if confirmed_bar in highs_set:
            active_sh_idx = confirmed_bar
        if confirmed_bar in lows_set:
            active_sl_idx = confirmed_bar

        # Check Bullish Break of last confirmed Swing High
        if active_sh_idx > 0 and active_sh_idx not in broken_sh_set and i > active_sh_idx:
            sh_lvl = float(highs[active_sh_idx])
            if closes[i] > sh_lvl + buf:
                broken_sh_set.add(active_sh_idx)
                kind = "BOS" if is_bull else "CHoCH"
                # Origin low is the lowest point between the broken Swing High and the break candle i
                origin_low_idx = active_sh_idx + int(np.argmin(lows[active_sh_idx : i + 1]))
                is_bull = True
                structure_breaks.append({
                    "kind":        kind,
                    "direction":   "BULLISH",
                    "bullish":     True,
                    "level":       round(sh_lvl, decimals),
                    "swing_idx":   int(active_sh_idx),
                    "start_time":  int(dates[active_sh_idx]),
                    "break_idx":   int(i),
                    "end_time":    int(dates[i]),
                    "origin_idx":  int(origin_low_idx),
                    "origin_time": int(dates[origin_low_idx]),
                    "origin_price": float(lows[origin_low_idx]),
                })

        # Check Bearish Break of last confirmed Swing Low
        if active_sl_idx > 0 and active_sl_idx not in broken_sl_set and i > active_sl_idx:
            sl_lvl = float(lows[active_sl_idx])
            if closes[i] < sl_lvl - buf:
                broken_sl_set.add(active_sl_idx)
                kind = "BOS" if (not is_bull) else "CHoCH"
                # Origin high is the highest point between the broken Swing Low and the break candle i
                origin_high_idx = active_sl_idx + int(np.argmax(highs[active_sl_idx : i + 1]))
                is_bull = False
                structure_breaks.append({
                    "kind":        kind,
                    "direction":   "BEARISH",
                    "bullish":     False,
                    "level":       round(sl_lvl, decimals),
                    "swing_idx":   int(active_sl_idx),
                    "start_time":  int(dates[active_sl_idx]),
                    "break_idx":   int(i),
                    "end_time":    int(dates[i]),
                    "origin_idx":  int(origin_high_idx),
                    "origin_time": int(dates[origin_high_idx]),
                    "origin_price": float(highs[origin_high_idx]),
                })

    # ── Select Active Recent Structure Break (Focus on the Recent Trend!) ───
    if structure_breaks:
        latest_break = structure_breaks[-1]
        is_bullish   = latest_break["bullish"]
        break_kind   = latest_break["kind"]
        anchor_idx   = latest_break["origin_idx"]
        anchor_time  = latest_break["origin_time"]
        anchor_price = latest_break["origin_price"]
        break_idx    = latest_break["break_idx"]
    else:
        is_bullish   = is_bull
        break_kind   = "BOS"
        anchor_idx   = int(np.argmin(lows[-80:])) + max(0, total - 80) if is_bullish else int(np.argmax(highs[-80:])) + max(0, total - 80)
        anchor_time  = int(dates[anchor_idx])
        anchor_price = float(lows[anchor_idx]) if is_bullish else float(highs[anchor_idx])
        break_idx    = min(total - 1, anchor_idx + 5)

    # ── Detect Ranging Market (Equal Highs & Equal Lows Compression) ────────
    recent_window = min(45, total)
    recent_range  = float(np.max(highs[-recent_window:]) - np.min(lows[-recent_window:]))
    bars_since_break = (total - 1) - break_idx
    is_ranging = bool(recent_range < (2.0 * atr) and bars_since_break > 35)

    # ── Locate Origin POI Order Block + Mandatory 3-Candle FVG ──────────────
    poi_data = find_origin_poi_with_fvg(
        df, is_bullish, anchor_idx, break_idx, atr, decimals
    )

    # If the Origin POI was already completely violated (broken through), advance to most recent intact swing origin
    poi_violated = False
    if is_bullish and closes[-1] < poi_data["poi_bottom"] - (0.15 * atr):
        poi_violated = True
    elif (not is_bullish) and closes[-1] > poi_data["poi_top"] + (0.15 * atr):
        poi_violated = True

    if poi_violated and len(structure_breaks) >= 2:
        # Check if price is still respecting the macro origin
        prev_same = [b for b in structure_breaks[:-1] if b["bullish"] == is_bullish]
        if prev_same:
            alt_b = prev_same[-1]
            alt_poi = find_origin_poi_with_fvg(df, is_bullish, alt_b["origin_idx"], alt_b["break_idx"], atr, decimals)
            if (is_bullish and closes[-1] >= alt_poi["poi_bottom"]) or ((not is_bullish) and closes[-1] <= alt_poi["poi_top"]):
                poi_data     = alt_poi
                anchor_idx   = alt_b["origin_idx"]
                anchor_time  = alt_b["origin_time"]
                anchor_price = alt_b["origin_price"]
                break_idx    = alt_b["break_idx"]

    poi_top    = poi_data["poi_top"]
    poi_bottom = poi_data["poi_bottom"]
    poi_idx    = poi_data["poi_idx"]
    poi_time   = poi_data["poi_time"]
    has_fvg    = poi_data["has_fvg"]

    # ── Confluence #4: Liquidity / Inducement Before POI ────────────────────
    liq_data = find_liquidity_before_poi(
        df, is_bullish, poi_idx, poi_top, poi_bottom, raw_highs, raw_lows, atr, decimals
    )

    # ── Build Chart Zone Boxes (Origin POI + FVG + Liquidity Zone) ──────────
    zone_boxes = []
    if is_bullish:
        poi_label = f"BUY POI / OB ({poi_bottom:.{decimals}f}–{poi_top:.{decimals}f})" + (" + FVG ✅" if has_fvg else "")
        zone_boxes.append({
            "type":      "BULL_OB",
            "top":       poi_top,
            "bottom":    poi_bottom,
            "time":      poi_time,
            "end_time":  int(dates[-1]),
            "label":     poi_label,
            "mitigated": False,
        })
    else:
        poi_label = f"SELL POI / OB ({poi_bottom:.{decimals}f}–{poi_top:.{decimals}f})" + (" + FVG ✅" if has_fvg else "")
        zone_boxes.append({
            "type":      "BEAR_OB",
            "top":       poi_top,
            "bottom":    poi_bottom,
            "time":      poi_time,
            "end_time":  int(dates[-1]),
            "label":     poi_label,
            "mitigated": False,
        })

    if has_fvg and poi_data["fvg_size"] > 0:
        zone_boxes.append({
            "type":      "BULL_FVG" if is_bullish else "BEAR_FVG",
            "top":       poi_data["fvg_top"],
            "bottom":    poi_data["fvg_bottom"],
            "time":      poi_data["fvg_time"],
            "end_time":  int(dates[-1]),
            "label":     f"FVG ({poi_data['fvg_bottom']:.{decimals}f}–{poi_data['fvg_top']:.{decimals}f})",
            "mitigated": False,
        })

    if liq_data.get("liq_box"):
        zone_boxes.append(liq_data["liq_box"])

    poi_level = poi_top if is_bullish else poi_bottom
    closed_price_val = float(closes[-1])

    targets = find_structural_targets(
        df, raw_highs, raw_lows, is_bullish, poi_level, int(anchor_idx), anchor_price, atr, decimals
    )

    # ── Build BOS / CHoCH Structure Lines + Target Lines ────────────────────
    all_lines = []
    recent_breaks = structure_breaks[-3:] if len(structure_breaks) >= 3 else structure_breaks
    for sb in recent_breaks:
        sb_bull  = sb["bullish"]
        sb_kind  = sb["kind"]
        sb_arrow = "▲" if sb_bull else "▼"
        sb_color = (
            "rgba(0, 230, 118, 0.95)" if (sb_bull and sb_kind == "CHoCH")
            else "rgba(38, 166, 154, 0.85)" if sb_bull
            else "rgba(255, 82, 82, 0.95)" if sb_kind == "CHoCH"
            else "rgba(239, 83, 80, 0.85)"
        )
        all_lines.append({
            "level":      sb["level"],
            "start_time": sb["start_time"],
            "end_time":   sb["end_time"],
            "type":       f"{sb_kind} {sb_arrow} ({sb['level']:.{decimals}f})",
            "color":      sb_color,
            "is_struct":  True,
            "is_choch":   sb_kind == "CHoCH",
        })

    target_line_color = "rgba(255, 193, 7, 0.95)"
    for t in targets:
        all_lines.append({
            "level":      t["level"],
            "start_time": t["start_time"],
            "end_time":   t["end_time"],
            "type":       t["label"],
            "color":      target_line_color,
            "is_struct":  False,
            "is_choch":   False,
        })

    step_seconds = int(dates[-1] - dates[-2]) if len(dates) >= 2 else 3600
    projection_path = [
        {"time": int(dates[-1]), "price": round(closed_price_val, decimals), "label": "Now"},
        {"time": int(dates[-1] + step_seconds * 4), "price": round(poi_level, decimals), "label": "POI Entry Zone"},
        {"time": int(dates[-1] + step_seconds * 16), "price": targets[-1]["level"], "label": targets[-1]["label"]},
    ]

    if is_ranging:
        cycle = "RANGING MARKET (Equal H/L)"
    elif is_bullish:
        cycle = f"BULLISH STRUCTURE ({break_kind} ▲)"
    else:
        cycle = f"BEARISH STRUCTURE ({break_kind} ▼)"

    sweeps = detect_liquidity_sweeps(df, raw_highs, raw_lows, atr)
    structure_confirmed = bool(len(structure_breaks) > 0 and not is_ranging)

    return {
        "cycle":               cycle,
        "is_bullish":          is_bullish,
        "is_ranging":          is_ranging,
        "break_kind":          break_kind,
        "latest_break":        structure_breaks[-1] if structure_breaks else None,
        "lines":               all_lines,
        "anchor":              anchor_price,
        "anchor_idx":          int(anchor_idx),
        "break_idx":           int(break_idx),
        "atr":                 atr,
        "peak_confirmed":      structure_confirmed,
        "poi_data":            poi_data,
        "liq_data":            liq_data,
        "sweeps":              sweeps,
        "swing_order":         swing_order,
        "raw_highs":           raw_highs.tolist(),
        "raw_lows":            raw_lows.tolist(),
        "trendline":           None,
        "zone_boxes":          zone_boxes,
        "poi_level":           round(poi_level, decimals),
        "targets":             targets,
        "projection_path":     projection_path,
    }


def evaluate_entry_signal(df: pd.DataFrame, full_df: pd.DataFrame, ms: Dict, current_price: float,
                          ema_50: float, ema_200: float, rsi: float,
                          htf_aligned: bool, decimals: int, pip_size: float,
                          state_key: Optional[str] = None) -> Dict:
    is_bullish     = ms["is_bullish"]
    is_ranging     = ms.get("is_ranging", False)
    break_kind     = ms.get("break_kind", "BOS")
    latest_break   = ms.get("latest_break")
    peak_confirmed = ms["peak_confirmed"]
    anchor_price   = ms["anchor"]
    anchor_idx     = ms["anchor_idx"]
    break_idx      = ms.get("break_idx", anchor_idx)
    atr            = ms["atr"]
    poi_data       = ms["poi_data"]
    liq_data       = ms["liq_data"]
    zone_boxes     = ms["zone_boxes"]
    poi_level      = ms["poi_level"]
    targets        = ms["targets"]

    opens  = df['Open'].values
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(closes)

    active_ob = zone_boxes[0]
    t1_level  = targets[0]["level"]
    t2_level  = targets[1]["level"]

    has_fvg       = bool(poi_data.get("has_fvg", False))
    has_liquidity = bool(liq_data.get("has_liquidity", False))
    liq_swept     = bool(liq_data.get("swept", False))
    liq_type      = liq_data.get("liq_type", "Inducement")
    liq_level     = liq_data.get("liq_level")
    break_lvl_str = f"{latest_break['level']:.{decimals}f}" if latest_break else f"{anchor_price:.{decimals}f}"

    # ── 0. RANGING MARKET FILTER ("Fold your hands and watch!") ─────────────
    if is_ranging:
        checklist = [
            {"step": "1. Market Structure (BOS / CHoCH)", "done": False, "detail": "RANGING (Equal H/L)"},
            {"step": "2. Origin POI + Fair Value Gap (FVG)", "done": False, "detail": "Wait for Breakout"},
            {"step": "3. Liquidity (IDM / Range) Swept", "done": False, "detail": "Waiting"},
            {"step": "4. 1H Rejection Close in POI", "done": False, "detail": "Fold Hands & Watch"},
        ]
        note = "⏸️ RANGING MARKET DETECTED (Equal Highs & Equal Lows): No clear directional structure. Fold your hands and wait for a clean BOS/CHoCH breakout."
        return {
            "signal":     "WAIT",
            "confidence": 35,
            "setup_name": "Ranging Market — Fold Hands & Watch",
            "checklist":  checklist,
            "execution_plan": {
                "stage":             "AWAITING_PULLBACK",
                "trigger_condition": note,
                "planned_entry":     round(poi_level, decimals),
                "planned_sl":        round(active_ob["bottom"] - 0.35 * atr if is_bullish else active_ob["top"] + 0.35 * atr, decimals),
                "planned_tp1":       round(t1_level, decimals),
                "planned_tp2":       round(t2_level, decimals),
                "risk_reward":       0.0,
            },
            "entry_note":   note,
            "trade_levels": None,
        }

    # ── 1. CHECK IN-MEMORY FROZEN TRADE VAULT FIRST ─────────────────────────
    if state_key and state_key in ACTIVE_TRADES:
        lt = ACTIVE_TRADES[state_key]
        live_high = max(float(full_df['High'].iloc[-1]), current_price)
        live_low  = min(float(full_df['Low'].iloc[-1]), current_price)
        closed_out = False
        if lt["is_bullish"] != is_bullish:
            closed_out = True
        elif lt["is_bullish"] and (live_low <= lt["stop_loss"] or live_high >= lt["target_1"]):
            closed_out = True
        elif (not lt["is_bullish"]) and (live_high >= lt["stop_loss"] or live_low <= lt["target_1"]):
            closed_out = True

        if closed_out:
            del ACTIVE_TRADES[state_key]

    # ── 2. SCAN FOR A DETERMINISTIC, UNCLOSED 1H SIGNAL CANDLE IN THE POI ───
    locked_trade = ACTIVE_TRADES.get(state_key) if state_key else None
    poi_idx      = int(poi_data.get("poi_idx", anchor_idx))

    if locked_trade is None and peak_confirmed and total > max(poi_idx, break_idx) + 1:
        search_start = max(poi_idx + 2, break_idx + 1, total - 35)
        for k in range(search_start, total):
            c_open  = float(opens[k])
            c_close = float(closes[k])
            c_high  = float(highs[k])
            c_low   = float(lows[k])
            p_high  = float(highs[k - 1])
            p_low   = float(lows[k - 1])

            body_sz = abs(c_close - c_open)
            u_wick  = c_high - max(c_open, c_close)
            l_wick  = min(c_open, c_close) - c_low

            if is_bullish:
                # Price taps into the BUY POI Order Block (or attached FVG) and holds above OB bottom
                tap_ceil   = max(active_ob["top"], poi_data.get("fvg_top", active_ob["top"]))
                touched_ob = (min(c_low, p_low) <= tap_ceil + 0.12 * atr) and (c_close >= active_ob["bottom"] - 0.05 * atr)
                if not touched_ob:
                    continue

                is_bull_rej = (
                    ((c_close > c_open) and (body_sz >= 0.10 * atr))
                    or (l_wick >= 0.18 * atr and c_close >= c_low + 0.55 * max(c_high - c_low, 1e-9))
                )
                not_chasing = (c_close - active_ob["top"]) <= (0.65 * atr)
                if not (is_bull_rej and not_chasing):
                    continue

                entry_k = round(c_close, decimals)
                floor_k = min(active_ob["bottom"], c_low, p_low)
                sl_k    = round(min(floor_k - 0.28 * atr, entry_k - 0.45 * atr), decimals)
                risk_k  = max(abs(entry_k - sl_k), 1e-6)

                t_list  = find_structural_targets(df.iloc[:k + 1], ms["raw_highs"], ms["raw_lows"], True, entry_k, anchor_idx, anchor_price, atr, decimals)
                if len(t_list) < 2:
                    continue
                tp1_k   = t_list[0]["level"]
                tp2_k   = t_list[1]["level"]
                rr_k    = round(max(abs(tp1_k - entry_k), abs(tp2_k - entry_k)) / risk_k, 2)
                if rr_k < MIN_ALLOWED_RR:
                    continue

                post_lows  = list(lows[k + 1:]) + [float(full_df['Low'].iloc[-1]), current_price]
                post_highs = list(highs[k + 1:]) + [float(full_df['High'].iloc[-1]), current_price]
                if min(post_lows) <= sl_k or max(post_highs) >= tp1_k:
                    continue

                locked_trade = {
                    "is_bullish":  True,
                    "signal":      "BUY",
                    "signal_idx":  k,
                    "signal_time": int(dates[k]),
                    "entry":       entry_k,
                    "stop_loss":   sl_k,
                    "target_1":    tp1_k,
                    "target_2":    tp2_k,
                    "risk_reward": rr_k,
                    "zone_name":   "Origin Buy POI + FVG" if has_fvg else "Origin Buy POI",
                }
                if state_key:
                    ACTIVE_TRADES[state_key] = locked_trade
                break

            else:
                tap_floor  = min(active_ob["bottom"], poi_data.get("fvg_bottom", active_ob["bottom"]))
                touched_ob = (max(c_high, p_high) >= tap_floor - 0.12 * atr) and (c_close <= active_ob["top"] + 0.05 * atr)
                if not touched_ob:
                    continue

                is_bear_rej = (
                    ((c_close < c_open) and (body_sz >= 0.10 * atr))
                    or (u_wick >= 0.18 * atr and c_close <= c_high - 0.55 * max(c_high - c_low, 1e-9))
                )
                not_chasing = (active_ob["bottom"] - c_close) <= (0.65 * atr)
                if not (is_bear_rej and not_chasing):
                    continue

                entry_k = round(c_close, decimals)
                ceil_k  = max(active_ob["top"], c_high, p_high)
                sl_k    = round(max(ceil_k + 0.28 * atr, entry_k + 0.45 * atr), decimals)
                risk_k  = max(abs(sl_k - entry_k), 1e-6)

                t_list  = find_structural_targets(df.iloc[:k + 1], ms["raw_highs"], ms["raw_lows"], False, entry_k, anchor_idx, anchor_price, atr, decimals)
                if len(t_list) < 2:
                    continue
                tp1_k   = t_list[0]["level"]
                tp2_k   = t_list[1]["level"]
                rr_k    = round(max(abs(entry_k - tp1_k), abs(entry_k - tp2_k)) / risk_k, 2)
                if rr_k < MIN_ALLOWED_RR:
                    continue

                post_lows  = list(lows[k + 1:]) + [float(full_df['Low'].iloc[-1]), current_price]
                post_highs = list(highs[k + 1:]) + [float(full_df['High'].iloc[-1]), current_price]
                if max(post_highs) >= sl_k or min(post_lows) <= tp1_k:
                    continue

                locked_trade = {
                    "is_bullish":  False,
                    "signal":      "SELL",
                    "signal_idx":  k,
                    "signal_time": int(dates[k]),
                    "entry":       entry_k,
                    "stop_loss":   sl_k,
                    "target_1":    tp1_k,
                    "target_2":    tp2_k,
                    "risk_reward": rr_k,
                    "zone_name":   "Origin Sell POI + FVG" if has_fvg else "Origin Sell POI",
                }
                if state_key:
                    ACTIVE_TRADES[state_key] = locked_trade
                break

    # ── 3. IF AN ACTIVE NON-REPAINTING TRADE IS LOCKED ──────────────────────
    if locked_trade:
        f_entry = locked_trade["entry"]
        f_sl    = locked_trade["stop_loss"]
        f_tp1   = locked_trade["target_1"]
        f_tp2   = locked_trade["target_2"]
        f_rr    = locked_trade["risk_reward"]
        f_dir   = locked_trade["signal"]
        z_name  = locked_trade["zone_name"]

        for line in ms["lines"]:
            if line["type"].startswith("Target 1"):
                line["level"] = f_tp1
                line["type"]  = f"Target 1 ({f_tp1:.{decimals}f})"
            elif line["type"].startswith("Target 2"):
                line["level"] = f_tp2
                line["type"]  = f"Target 2 ({f_tp2:.{decimals}f})"

        step_seconds = int(dates[-1] - dates[-2]) if len(dates) >= 2 else 3600
        now_ts = int(dates[-1])
        ms["projection_path"] = [
            {"time": now_ts, "price": f_entry, "label": "Entry"},
            {"time": now_ts + step_seconds * 4, "price": f_tp1, "label": "TP1"},
            {"time": now_ts + step_seconds * 8, "price": f_tp2, "label": "TP2"},
        ]

        checklist = [
            {"step": f"1. Market Structure ({break_kind} {'▲' if is_bullish else '▼'})", "done": True, "detail": f"Cleared {break_lvl_str} ✅"},
            {"step": "2. Origin POI + Fair Value Gap (FVG)", "done": True, "detail": f"{active_ob['bottom']:.{decimals}f}–{active_ob['top']:.{decimals}f} ✅"},
            {"step": f"3. Liquidity ({liq_type}) Swept", "done": True, "detail": f"Swept into POI ✅"},
            {"step": f"4. 1H POI Rejection (RR 1:{f_rr})", "done": True, "detail": f"Entered @ {f_entry:.{decimals}f} ✅"},
        ]

        # Base 85% + bonuses for FVG, Liquidity Sweep, and HTF alignment
        active_conf = 80 + (5 if has_fvg else 0) + (5 if has_liquidity else 0) + (4 if htf_aligned else 0)

        trigger_note = (
            f"{'🟢 BUY' if is_bullish else '🔴 SELL'} EXECUTING: Locked Entry @ {f_entry:.{decimals}f} ({z_name}) | "
            f"Target 1: {f_tp1:.{decimals}f} | Target 2: {f_tp2:.{decimals}f} | SL: {f_sl:.{decimals}f}"
        )

        return {
            "signal":     f_dir,
            "confidence": min(96, active_conf),
            "setup_name": f"SMC A+ {z_name}",
            "checklist":  checklist,
            "execution_plan": {
                "stage":             "TRADE_ACTIVE",
                "trigger_condition": trigger_note,
                "planned_entry":     f_entry,
                "planned_sl":        f_sl,
                "planned_tp1":       f_tp1,
                "planned_tp2":       f_tp2,
                "risk_reward":       f_rr,
            },
            "entry_note": trigger_note,
            "trade_levels": {
                "entry":       f_entry,
                "stop_loss":   f_sl,
                "take_profit": f_tp1,
                "target_1":    f_tp1,
                "target_2":    f_tp2,
                "risk_reward": f_rr,
            }
        }

    # ── 4. WAITING FOR POI TAP OR CONFIRMING IN POI ZONE ────────────────────
    live_high = max(float(full_df['High'].iloc[-1]), current_price)
    live_low  = min(float(full_df['Low'].iloc[-1]), current_price)
    live_open = float(full_df['Open'].iloc[-1])

    if is_bullish:
        in_zone_now = (live_low <= poi_level + 0.12 * atr) and (current_price >= active_ob["bottom"] - 0.10 * atr)
        planned_sl  = round(min(active_ob["bottom"], poi_level) - (0.30 * atr), decimals)
        risk        = max(abs(poi_level - planned_sl), 1e-6)
        best_rr     = round(max(abs(t1_level - poi_level), abs(t2_level - poi_level)) / risk, 2)

        liq_detail = (
            f"Swept {liq_level:.{decimals}f} ✅" if (has_liquidity and (liq_swept or in_zone_now))
            else f"Sweep {liq_level:.{decimals}f} first" if has_liquidity
            else "Building above POI"
        )

        checklist = [
            {"step": f"1. Bullish {break_kind} ▲ Structure", "done": bool(peak_confirmed), "detail": f"Cleared {break_lvl_str}"},
            {"step": "2. Origin Buy POI + FVG", "done": bool(has_fvg), "detail": f"{poi_level:.{decimals}f}" + (" (FVG ✅)" if has_fvg else " (No FVG)")},
            {"step": f"3. {liq_type} Before POI", "done": bool(has_liquidity and (liq_swept or in_zone_now)), "detail": liq_detail},
            {"step": f"4. 1H Rejection in POI (RR 1:{best_rr})", "done": False, "detail": "Watch 1H Close!" if in_zone_now else f"Wait for {poi_level:.{decimals}f}"},
        ]

        if in_zone_now:
            stage = "IN_ZONE_CONFIRMING"
            trig_lvl = round(max(live_open, poi_level), decimals)
            note = f"🎯 PRICE TAPPED BUY POI ({poi_level:.{decimals}f})! Wait for the current 1H candle to close GREEN above {trig_lvl:.{decimals}f} to lock BUY."
        else:
            stage = "AWAITING_PULLBACK"
            pips_away = round(abs(current_price - poi_level) / max(pip_size, 1e-6), 1)
            liq_hint = f" Watch for price to sweep {liq_type} ({liq_level:.{decimals}f}) into the POI." if has_liquidity and not liq_swept else ""
            note = f"⏳ AWAITING PULLBACK TO ORIGIN POI: Price is {pips_away} pips above Buy POI ({poi_level:.{decimals}f}).{liq_hint}"

        wait_conf = 68 if in_zone_now else (56 if (has_fvg and has_liquidity) else 48)

        return {
            "signal":     "WAIT",
            "confidence": wait_conf,
            "setup_name": "SMC Bullish A+ — " + ("In Buy POI" if in_zone_now else "Awaiting POI Tap"),
            "checklist":  checklist,
            "execution_plan": {
                "stage":             stage,
                "trigger_condition": note,
                "planned_entry":     round(poi_level, decimals),
                "planned_sl":        planned_sl,
                "planned_tp1":       round(t1_level, decimals),
                "planned_tp2":       round(t2_level, decimals),
                "risk_reward":       best_rr,
            },
            "entry_note":   note,
            "trade_levels": None
        }

    else:
        in_zone_now = (live_high >= poi_level - 0.12 * atr) and (current_price <= active_ob["top"] + 0.10 * atr)
        planned_sl  = round(max(active_ob["top"], poi_level) + (0.30 * atr), decimals)
        risk        = max(abs(planned_sl - poi_level), 1e-6)
        best_rr     = round(max(abs(poi_level - t1_level), abs(poi_level - t2_level)) / risk, 2)

        liq_detail = (
            f"Swept {liq_level:.{decimals}f} ✅" if (has_liquidity and (liq_swept or in_zone_now))
            else f"Sweep {liq_level:.{decimals}f} first" if has_liquidity
            else "Building below POI"
        )

        checklist = [
            {"step": f"1. Bearish {break_kind} ▼ Structure", "done": bool(peak_confirmed), "detail": f"Cleared {break_lvl_str}"},
            {"step": "2. Origin Sell POI + FVG", "done": bool(has_fvg), "detail": f"{poi_level:.{decimals}f}" + (" (FVG ✅)" if has_fvg else " (No FVG)")},
            {"step": f"3. {liq_type} Before POI", "done": bool(has_liquidity and (liq_swept or in_zone_now)), "detail": liq_detail},
            {"step": f"4. 1H Rejection in POI (RR 1:{best_rr})", "done": False, "detail": "Watch 1H Close!" if in_zone_now else f"Wait for {poi_level:.{decimals}f}"},
        ]

        if in_zone_now:
            stage = "IN_ZONE_CONFIRMING"
            trig_lvl = round(min(live_open, poi_level), decimals)
            note = f"🎯 PRICE TAPPED SELL POI ({poi_level:.{decimals}f})! Wait for the current 1H candle to close RED below {trig_lvl:.{decimals}f} to lock SELL."
        else:
            stage = "AWAITING_PULLBACK"
            pips_away = round(abs(poi_level - current_price) / max(pip_size, 1e-6), 1)
            liq_hint = f" Watch for price to sweep {liq_type} ({liq_level:.{decimals}f}) into the POI." if has_liquidity and not liq_swept else ""
            note = f"⏳ AWAITING RALLY TO ORIGIN POI: Price is {pips_away} pips below Sell POI ({poi_level:.{decimals}f}).{liq_hint}"

        wait_conf = 68 if in_zone_now else (56 if (has_fvg and has_liquidity) else 48)

        return {
            "signal":     "WAIT",
            "confidence": wait_conf,
            "setup_name": "SMC Bearish A+ — " + ("In Sell POI" if in_zone_now else "Awaiting POI Tap"),
            "checklist":  checklist,
            "execution_plan": {
                "stage":             stage,
                "trigger_condition": note,
                "planned_entry":     round(poi_level, decimals),
                "planned_sl":        planned_sl,
                "planned_tp1":       round(t1_level, decimals),
                "planned_tp2":       round(t2_level, decimals),
                "risk_reward":       best_rr,
            },
            "entry_note":   note,
            "trade_levels": None
        }


# ─────────────────────────────────────────────────────────────────────────────
# API ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

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
        current_price  = req.current_price if req.current_price > 0 else csv_last_price

        profile  = get_instrument_profile(req.currency, current_price)
        decimals = profile['decimals']
        pip_size = profile['pip_size']

        ema_200 = safe_float(EMAIndicator(close=df['Close'], window=200).ema_indicator().iloc[-1], current_price)
        ema_50  = safe_float(EMAIndicator(close=df['Close'], window=50).ema_indicator().iloc[-1], current_price)
        rsi     = safe_float(RSIIndicator(close=df['Close'], window=14).rsi().iloc[-1], 50.0)

        state_key = f"{req.currency}:{req.timeframe}"
        ms = analyze_market_structure(
            df, profile,
            state_key=state_key,
            drop_forming=(DROP_FORMING_CANDLE and data_source == "LIVE_NODE_DATA"),
        )
        cycle          = ms['cycle']
        lines          = ms['lines']
        sweeps         = ms['sweeps']
        peak_confirmed = ms.get('peak_confirmed', False)
        trendline_data = ms.get('trendline')
        zone_boxes     = ms.get('zone_boxes', [])
        targets        = ms.get('targets', [])

        bias_str = 'NEUTRAL' if ms.get('is_ranging') else ('BULLISH' if ms['is_bullish'] else 'BEARISH')

        htf_aligned = False
        htf_bias_str = "NEUTRAL"
        if req.htf_candles and len(req.htf_candles) > 50:
            htf_df = process_live_candles(req.htf_candles)
            if htf_df is not None:
                htf_ms = analyze_market_structure(htf_df, profile, drop_forming=False)
                htf_bias_str = "NEUTRAL" if htf_ms.get("is_ranging") else ("BULLISH" if htf_ms["is_bullish"] else "BEARISH")
                htf_aligned = (htf_bias_str == bias_str) and (bias_str != "NEUTRAL")
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

        t1_out = entry_eval["trade_levels"]["target_1"] if entry_eval["trade_levels"] else targets[0]["level"]
        t2_out = entry_eval["trade_levels"]["target_2"] if entry_eval["trade_levels"] else targets[1]["level"]

        return {
            "signal":         entry_eval["signal"],
            "confidence":     entry_eval["confidence"],
            "trend":          cycle,
            "pattern":        entry_eval["setup_name"],
            "peak_confirmed": peak_confirmed,
            "checklist":      checklist,
            "executionPlan":  entry_eval["execution_plan"],
            "reasoning":      reasoning,
            "keyLevels": {
                "resistance": t2_out if ms["is_bullish"] else round(ms["anchor"], decimals),
                "support":    round(ms["anchor"], decimals) if ms["is_bullish"] else t2_out,
                "poi":        ms["poi_level"],
                "target1":    t1_out,
                "target2":    t2_out,
                "ema200":     round(ema_200, decimals),
                "ema50":      round(ema_50, decimals),
            },
            "visuals": {
                "bos_lines":       lines,
                "sweeps":          sweeps,
                "trendline":       trendline_data,
                "order_blocks":    zone_boxes,
                "targets":         targets,
                "projection_path": ms["projection_path"],
            },
            "mtf_confluence": [
                {"tf": "4H", "bias": htf_bias_str, "strength": 88 if htf_aligned else 50},
                {"tf": "1H", "bias": bias_str, "strength": 85 if (peak_confirmed and ms["poi_data"]["has_fvg"]) else 60},
            ],
            "tradeSetup":     entry_eval["trade_levels"],
            "dataSource":     data_source,
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
        ms = analyze_market_structure(df, profile)
        return {
            "✅ ENGINE VERSION": "AuraBrain v7.0 (SMC 4-Confluence A+ Sniper)",
            "📊 INSTRUMENT":     req.currency,
            "🎯 STRUCTURE":      ms['cycle'],
            "⚓ ORIGIN ANCHOR":  ms['anchor'],
            "📦 POI + FVG":      ms['poi_data'],
            "💧 LIQUIDITY":      ms['liq_data'],
            "🎯 TARGETS":        ms.get('targets', []),
            "🔒 ACTIVE TRADES":  ACTIVE_TRADES,
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