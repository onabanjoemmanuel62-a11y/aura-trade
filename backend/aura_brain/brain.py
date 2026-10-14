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
# PEAK FORMATION + SMC 4-CONFLUENCE A+ STRATEGY TUNING
# ─────────────────────────────────────────────────────────────────────────────
BASE_SWING_ORDER             = 4
FLIP_BUFFER_ATR              = 0.20
FLIP_CONFIRM_BARS            = 2
ANCHOR_MIN_MOVE_ATR          = 1.4
SWING_PROMINENCE_ATR         = 0.80
MIN_FVG_SIZE_ATR             = 0.06
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
# CORE ENGINE — Peak Formation + 4-Confluence SMC Strategy (BOS/CHoCH + POI + FVG + IDM)
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
                            is_bullish: bool, poi_ref: float, current_price: float,
                            anchor_idx: int, anchor_price: float,
                            atr: float, decimals: int) -> List[Dict]:
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(highs)
    if total == 0:
        return []

    end_time    = int(dates[-1])
    anchor_idx  = int(np.clip(anchor_idx, 0, total - 1))
    anchor_time = int(dates[anchor_idx])

    min_dist_from_poi = 1.20 * atr
    min_separation    = 1.10 * atr
    targets = []

    if is_bullish:
        leg_high = float(max(highs[anchor_idx:].max(), current_price))
        min_t1_floor = max(poi_ref + min_dist_from_poi, leg_high)

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
            valid_candidates.append((max(lvl, min_t1_floor), t_start))

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
            if t_num == 1:
                proj_lvl = round(min_t1_floor, decimals)
            else:
                proj_lvl = round(targets[-1]["level"] + (1.60 * atr), decimals)
            targets.append({
                "target_num": t_num,
                "level":      proj_lvl,
                "start_time": anchor_time,
                "end_time":   end_time,
                "label":      f"Target {t_num} ({proj_lvl:.{decimals}f})",
            })

    else:
        leg_low = float(min(lows[anchor_idx:].min(), current_price))
        max_t1_ceil = min(poi_ref - min_dist_from_poi, leg_low)

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
            valid_candidates.append((min(lvl, max_t1_ceil), t_start))

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
            if t_num == 1:
                proj_lvl = round(max_t1_ceil, decimals)
            else:
                proj_lvl = round(targets[-1]["level"] - (1.60 * atr), decimals)
            targets.append({
                "target_num": t_num,
                "level":      proj_lvl,
                "start_time": anchor_time,
                "end_time":   end_time,
                "label":      f"Target {t_num} ({proj_lvl:.{decimals}f})",
            })

    return targets


def build_origin_poi_zone(df: pd.DataFrame, raw_highs: np.ndarray, raw_lows: np.ndarray,
                          atr: float, is_bullish: bool, anchor_idx: int,
                          anchor_price: float, decimals: int) -> Dict:
    """
    VIDEO STRATEGY CONFLUENCE #2 & #3:
    1. Finds the Origin POI (either the Peak Formation extreme OR the most recent
       unmitigated structural Order Block swing after the Peak that broke structure).
    2. Refines the Order Block to the last opposite/reaction candle right before the
       impulse and verifies whether it created a 3-candle Fair Value Gap (FVG).
    3. Returns ONE clean POI Zone box (no separate giant yellow FVG box cluttering the chart!).
    """
    opens  = df['Open'].values
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(closes)

    anchor_idx     = int(np.clip(anchor_idx, 0, total - 1))
    zone_thickness = 0.38 * atr

    if is_bullish:
        chosen_idx = anchor_idx
        bot_val    = float(anchor_price)

        # Check if a newer structural Higher Low formed after the Peak and displaced >= 1.4 ATR
        post_swing_lows = [int(i) for i in raw_lows if anchor_idx + 3 <= int(i) <= total - 3]
        for sl_idx in reversed(post_swing_lows):
            sl_price = float(lows[sl_idx])
            if sl_price > anchor_price and (np.max(highs[sl_idx:]) - sl_price) >= (1.4 * atr):
                # Must remain unviolated by subsequent closes
                if np.min(closes[sl_idx:]) >= sl_price - (0.05 * atr) and closes[-1] > sl_price:
                    chosen_idx = sl_idx
                    bot_val    = sl_price
                    break

        # Refine inside the 3-candle window around chosen_idx to find the exact reaction candle before the impulse
        ob_idx  = chosen_idx
        has_fvg = False
        for k in range(chosen_idx, min(total - 2, chosen_idx + 4)):
            if lows[k + 2] - highs[k] >= (MIN_FVG_SIZE_ATR * atr) and closes[k + 1] > opens[k + 1]:
                has_fvg = True
                if float(lows[k]) <= bot_val + (0.45 * atr):
                    ob_idx = k
                break

        candle_top = float(max(opens[ob_idx], closes[ob_idx], highs[ob_idx]))
        top_val    = min(max(bot_val + 0.22 * atr, candle_top), bot_val + zone_thickness)

        # Find the first candle where price impulsively left the zone (departure bar)
        depart_idx = ob_idx + 1
        for m in range(ob_idx + 1, total):
            if lows[m] > top_val:
                depart_idx = m
                break

        bot_r = round(bot_val, decimals)
        top_r = round(top_val, decimals)
        label = f"BUY ZONE / POI ({bot_r:.{decimals}f}–{top_r:.{decimals}f})" + (" [FVG ✅]" if has_fvg else "")

        return {
            "poi_idx":    int(ob_idx),
            "depart_idx": int(depart_idx),
            "has_fvg":    has_fvg,
            "box": {
                "type":      "BULL_OB",
                "top":       top_r,
                "bottom":    bot_r,
                "time":      int(dates[ob_idx]),
                "end_time":  int(dates[-1]),
                "label":     label,
                "mitigated": False,
            }
        }

    else:
        chosen_idx = anchor_idx
        top_val    = float(anchor_price)

        # Check if a newer structural Lower High formed after the Peak and displaced >= 1.4 ATR
        post_swing_highs = [int(i) for i in raw_highs if anchor_idx + 3 <= int(i) <= total - 3]
        for sh_idx in reversed(post_swing_highs):
            sh_price = float(highs[sh_idx])
            if sh_price < anchor_price and (sh_price - np.min(lows[sh_idx:])) >= (1.4 * atr):
                if np.max(closes[sh_idx:]) <= sh_price + (0.05 * atr) and closes[-1] < sh_price:
                    chosen_idx = sh_idx
                    top_val    = sh_price
                    break

        ob_idx  = chosen_idx
        has_fvg = False
        for k in range(chosen_idx, min(total - 2, chosen_idx + 4)):
            if lows[k] - highs[k + 2] >= (MIN_FVG_SIZE_ATR * atr) and closes[k + 1] < opens[k + 1]:
                has_fvg = True
                if float(highs[k]) >= top_val - (0.45 * atr):
                    ob_idx = k
                break

        candle_bot = float(min(opens[ob_idx], closes[ob_idx], lows[ob_idx]))
        bot_val    = max(min(top_val - 0.22 * atr, candle_bot), top_val - zone_thickness)

        depart_idx = ob_idx + 1
        for m in range(ob_idx + 1, total):
            if highs[m] < bot_val:
                depart_idx = m
                break

        bot_r = round(bot_val, decimals)
        top_r = round(top_val, decimals)
        label = f"SELL ZONE / POI ({bot_r:.{decimals}f}–{top_r:.{decimals}f})" + (" [FVG ✅]" if has_fvg else "")

        return {
            "poi_idx":    int(ob_idx),
            "depart_idx": int(depart_idx),
            "has_fvg":    has_fvg,
            "box": {
                "type":      "BEAR_OB",
                "top":       top_r,
                "bottom":    bot_r,
                "time":      int(dates[ob_idx]),
                "end_time":  int(dates[-1]),
                "label":     label,
                "mitigated": False,
            }
        }


def find_pullback_liquidity(df: pd.DataFrame, is_bullish: bool, depart_idx: int,
                            poi_top: float, poi_bottom: float,
                            atr: float, decimals: int) -> Dict:
    """
    VIDEO STRATEGY CONFLUENCE #4 (Liquidity / Inducement Before POI):
    Strictly looks AFTER price has departed the POI (`j > depart_idx`) for a
    pullback reaction high/low (Inducement / Equal Highs-Lows) that formed
    in front of the POI without tapping it yet.
    """
    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(closes)

    if depart_idx >= total - 2:
        return {"has_liquidity": False, "swept": False, "liq_level": None, "liq_line": None, "liq_type": "Inducement (IDM)"}

    if is_bullish:
        cand_lows = []
        for j in range(depart_idx + 1, total - 1):
            if lows[j] <= lows[j - 1] and lows[j] <= lows[j + 1]:
                if poi_top + (0.18 * atr) <= lows[j] <= poi_top + (2.5 * atr):
                    cand_lows.append(j)

        if cand_lows:
            idm_idx   = min(cand_lows, key=lambda idx: lows[idx])
            idm_price = float(lows[idm_idx])
            eq_lows   = [idx for idx in cand_lows if abs(lows[idx] - idm_price) <= 0.20 * atr]
            liq_type  = "Range Liquidity (EQL)" if len(eq_lows) >= 2 else "Inducement (IDM)"
            post_min  = float(np.min(lows[idm_idx + 1:])) if idm_idx + 1 < total else idm_price
            swept     = bool(post_min < idm_price - (0.02 * atr))

            return {
                "has_liquidity": True,
                "swept":         swept,
                "liq_type":      liq_type,
                "liq_level":     round(idm_price, decimals),
                "liq_line": {
                    "level":      round(idm_price, decimals),
                    "start_time": int(dates[idm_idx]),
                    "end_time":   int(dates[-1]),
                    "type":       f"$$$ {liq_type} ({idm_price:.{decimals}f})",
                    "color":      "rgba(192, 132, 252, 0.90)",
                    "is_struct":  True,
                    "is_choch":   False,
                }
            }

    else:
        cand_highs = []
        for j in range(depart_idx + 1, total - 1):
            if highs[j] >= highs[j - 1] and highs[j] >= highs[j + 1]:
                if poi_bottom - (2.5 * atr) <= highs[j] <= poi_bottom - (0.18 * atr):
                    cand_highs.append(j)

        if cand_highs:
            idm_idx   = max(cand_highs, key=lambda idx: highs[idx])
            idm_price = float(highs[idm_idx])
            eq_highs  = [idx for idx in cand_highs if abs(highs[idx] - idm_price) <= 0.20 * atr]
            liq_type  = "Range Liquidity (EQH)" if len(eq_highs) >= 2 else "Inducement (IDM)"
            post_max  = float(np.max(highs[idm_idx + 1:])) if idm_idx + 1 < total else idm_price
            swept     = bool(post_max > idm_price + (0.02 * atr))

            return {
                "has_liquidity": True,
                "swept":         swept,
                "liq_type":      liq_type,
                "liq_level":     round(idm_price, decimals),
                "liq_line": {
                    "level":      round(idm_price, decimals),
                    "start_time": int(dates[idm_idx]),
                    "end_time":   int(dates[-1]),
                    "type":       f"$$$ {liq_type} ({idm_price:.{decimals}f})",
                    "color":      "rgba(192, 132, 252, 0.90)",
                    "is_struct":  True,
                    "is_choch":   False,
                }
            }

    return {"has_liquidity": False, "swept": False, "liq_level": None, "liq_line": None, "liq_type": "Inducement (IDM)"}


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
                             current_price: float = 0.0,
                             swing_order_override: Optional[int] = None,
                             state_key: Optional[str] = None,
                             drop_forming: bool = False) -> Dict:
    highs  = df['High'].values.copy()
    lows   = df['Low'].values.copy()
    closes = df['Close'].values.copy()
    dates  = df['Date'].values if 'Date' in df.columns else df.index.values
    total  = len(closes)
    decimals = profile.get('decimals', 5)

    if current_price > 0 and total > 0:
        closes[-1] = current_price
        highs[-1]  = max(highs[-1], current_price)
        lows[-1]   = min(lows[-1], current_price)

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

    # ── 1. Protected Swing Cycle Engine (Restores True Peak Formation High/Low!) ──
    def compute_structural_cycles(seed_bullish: bool):
        highs_set = set(raw_highs.tolist())
        lows_set  = set(raw_lows.tolist())

        is_bull = seed_bullish
        cycle_start_idx = 0
        extreme_price = highs[0] if is_bull else lows[0]

        protected_low_idx   = 0
        protected_high_idx  = 0
        last_confirmed_low  = None
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
                ema_crash = (
                    closes[i] < min(ema_50_array[i], ema_200_array[i]) - (1.2 * atr)
                    and (extreme_price - closes[i]) >= (2.8 * atr)
                )
                broke = struct_broke or ema_crash
                pending = pending + 1 if broke else 0

                if pending >= FLIP_CONFIRM_BARS or (i == total - 1 and broke and (extreme_price - closes[i]) >= 2.2 * atr):
                    true_peak_high_idx = cycle_start_idx + int(np.argmax(highs[cycle_start_idx : i + 1]))
                    broke_lvl          = float(lows[protected_low_idx])
                    broke_start_time   = int(dates[protected_low_idx])
                    is_bull            = False
                    cycle_start_idx    = true_peak_high_idx
                    extreme_price      = lows[i]
                    protected_high_idx = true_peak_high_idx
                    pending            = 0
                    events.append({
                        "flip_idx":         i,
                        "flip_time":        int(dates[i]),
                        "bullish":          False,
                        "anchor_idx":       true_peak_high_idx,
                        "anchor_time":      int(dates[true_peak_high_idx]),
                        "anchor_price":     float(highs[true_peak_high_idx]),
                        "choch_level":      round(broke_lvl, decimals),
                        "choch_start_time": broke_start_time,
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
                ema_surge = (
                    closes[i] > max(ema_50_array[i], ema_200_array[i]) + (1.2 * atr)
                    and (closes[i] - extreme_price) >= (2.8 * atr)
                )
                broke = struct_broke or ema_surge
                pending = pending + 1 if broke else 0

                if pending >= FLIP_CONFIRM_BARS or (i == total - 1 and broke and (closes[i] - extreme_price) >= 2.2 * atr):
                    true_peak_low_idx  = cycle_start_idx + int(np.argmin(lows[cycle_start_idx : i + 1]))
                    broke_lvl          = float(highs[protected_high_idx])
                    broke_start_time   = int(dates[protected_high_idx])
                    is_bull            = True
                    cycle_start_idx    = true_peak_low_idx
                    extreme_price      = highs[i]
                    protected_low_idx  = true_peak_low_idx
                    pending            = 0
                    events.append({
                        "flip_idx":         i,
                        "flip_time":        int(dates[i]),
                        "bullish":          True,
                        "anchor_idx":       true_peak_low_idx,
                        "anchor_time":      int(dates[true_peak_low_idx]),
                        "anchor_price":     float(lows[true_peak_low_idx]),
                        "choch_level":      round(broke_lvl, decimals),
                        "choch_start_time": broke_start_time,
                    })

        return events, is_bull

    seed_idx = min(50, total - 1)
    seed_bullish = bool(ema_50_array[seed_idx] > ema_200_array[seed_idx])
    cycle_events, is_bullish = compute_structural_cycles(seed_bullish)

    matching_events = [e for e in cycle_events if e["bullish"] == is_bullish]
    if matching_events:
        latest_event       = matching_events[-1]
        fresh_flip_idx     = latest_event["flip_idx"]
        fresh_flip_time    = latest_event["flip_time"]
        fresh_anchor_idx   = latest_event["anchor_idx"]
        fresh_anchor_time  = latest_event["anchor_time"]
        fresh_anchor_price = latest_event["anchor_price"]
        choch_level        = latest_event["choch_level"]
        choch_start_time   = latest_event["choch_start_time"]
    else:
        fresh_flip_idx     = max(0, total - 10)
        fresh_flip_time    = int(dates[fresh_flip_idx])
        fresh_anchor_idx   = int(np.argmin(lows)) if is_bullish else int(np.argmax(highs))
        fresh_anchor_time  = int(dates[fresh_anchor_idx])
        fresh_anchor_price = float(lows[fresh_anchor_idx]) if is_bullish else float(highs[fresh_anchor_idx])
        choch_level        = round(fresh_anchor_price, decimals)
        choch_start_time   = fresh_anchor_time

    # Advance macro anchor if older than 120 bars
    current_idx = total - 1
    if (current_idx - fresh_anchor_idx) > 120:
        dist_atr = abs(closes[-1] - fresh_anchor_price) / atr if atr > 0 else 0
        if dist_atr > 5.5:
            recent_start = max(0, current_idx - 110)
            if is_bullish:
                cand_lows = [
                    idx for idx in raw_lows
                    if recent_start <= idx <= current_idx - swing_order
                    and (highs[idx:].max() - lows[idx]) >= (ANCHOR_MIN_MOVE_ATR * atr)
                    and closes[-1] > lows[idx] + (1.0 * atr)
                ]
                if cand_lows:
                    new_idx = min(cand_lows, key=lambda idx: lows[idx])
                    fresh_anchor_idx   = int(new_idx)
                    fresh_anchor_time  = int(dates[fresh_anchor_idx])
                    fresh_anchor_price = float(lows[fresh_anchor_idx])
            else:
                cand_highs = [
                    idx for idx in raw_highs
                    if recent_start <= idx <= current_idx - swing_order
                    and (highs[idx] - lows[idx:].min()) >= (ANCHOR_MIN_MOVE_ATR * atr)
                    and closes[-1] < highs[idx] - (1.0 * atr)
                ]
                if cand_highs:
                    new_idx = max(cand_highs, key=lambda idx: highs[idx])
                    fresh_anchor_idx   = int(new_idx)
                    fresh_anchor_time  = int(dates[fresh_anchor_idx])
                    fresh_anchor_price = float(highs[fresh_anchor_idx])

    anchor_idx   = fresh_anchor_idx
    anchor_time  = fresh_anchor_time
    anchor_price = fresh_anchor_price

    if is_bullish:
        post_min_idx = anchor_idx + int(np.argmin(lows[anchor_idx:]))
        if lows[post_min_idx] < anchor_price:
            anchor_idx   = post_min_idx
            anchor_time  = int(dates[anchor_idx])
            anchor_price = float(lows[anchor_idx])
        move = float(highs[anchor_idx:].max() - anchor_price)
        extreme_held = bool(lows[anchor_idx:].min() >= anchor_price - 1e-9)
    else:
        post_max_idx = anchor_idx + int(np.argmax(highs[anchor_idx:]))
        if highs[post_max_idx] > anchor_price:
            anchor_idx   = post_max_idx
            anchor_time  = int(dates[anchor_idx])
            anchor_price = float(highs[anchor_idx])
        move = float(anchor_price - lows[anchor_idx:].min())
        extreme_held = bool(highs[anchor_idx:].max() <= anchor_price + 1e-9)

    peak_confirmed = bool(extreme_held and move >= ANCHOR_MIN_MOVE_ATR * atr)

    # Check if there was a subsequent BOS after the initial CHoCH flip inside this cycle
    break_kind       = "CHoCH"
    break_level      = choch_level
    break_start_time = choch_start_time
    break_end_time   = fresh_flip_time
    break_idx        = fresh_flip_idx

    if is_bullish:
        cycle_swings = [int(i) for i in raw_highs if anchor_idx < int(i) < total - 1]
        for sh_idx in cycle_swings:
            sh_val = float(highs[sh_idx])
            for m in range(sh_idx + 1, total):
                if closes[m] > sh_val + (0.15 * atr):
                    break_kind       = "BOS"
                    break_level      = round(sh_val, decimals)
                    break_start_time = int(dates[sh_idx])
                    break_end_time   = int(dates[m])
                    break_idx        = m
                    break
    else:
        cycle_swings = [int(i) for i in raw_lows if anchor_idx < int(i) < total - 1]
        for sl_idx in cycle_swings:
            sl_val = float(lows[sl_idx])
            for m in range(sl_idx + 1, total):
                if closes[m] < sl_val - (0.15 * atr):
                    break_kind       = "BOS"
                    break_level      = round(sl_val, decimals)
                    break_start_time = int(dates[sl_idx])
                    break_end_time   = int(dates[m])
                    break_idx        = m
                    break

    if not is_bullish:
        cycle        = f"BEARISH CYCLE (Peak M + {break_kind} ▼)"
        pattern_name = f"Peak Formation High ({anchor_price:.{decimals}f})"
        anchor_color = "rgba(255, 59, 59, 0.95)"
    else:
        cycle        = f"BULLISH CYCLE (Peak W + {break_kind} ▲)"
        pattern_name = f"Peak Formation Low ({anchor_price:.{decimals}f})"
        anchor_color = "rgba(59, 255, 130, 0.95)"

    # ── 2. Build Clean Origin POI Box (Validated by 3-Candle FVG) ───────────
    poi_info   = build_origin_poi_zone(df, raw_highs, raw_lows, atr, is_bullish, int(anchor_idx), anchor_price, decimals)
    active_ob  = poi_info["box"]
    zone_boxes = [active_ob]
    poi_idx    = poi_info["poi_idx"]
    depart_idx = poi_info["depart_idx"]
    has_fvg    = poi_info["has_fvg"]

    # ── 3. Strictly Unbroken Anchor-Pinned Demand/Supply Trendline ──────────
    trendline_data = None
    swing_pts_idx = raw_lows if is_bullish else raw_highs
    swing_pts_idx = swing_pts_idx[(swing_pts_idx > anchor_idx) & (swing_pts_idx <= current_idx)]

    if len(swing_pts_idx) >= 1 and current_idx > anchor_idx:
        x1 = int(anchor_idx)
        y1 = float(lows[x1]) if is_bullish else float(highs[x1])
        best_x2 = None
        best_slope = None

        for x2_cand in swing_pts_idx:
            x2_cand = int(x2_cand)
            if x2_cand <= x1 + 2:
                continue
            y2_cand = float(lows[x2_cand]) if is_bullish else float(highs[x2_cand])
            slope_cand = (y2_cand - y1) / (x2_cand - x1)

            if is_bullish and slope_cand <= 0:
                continue
            if (not is_bullish) and slope_cand >= 0:
                continue

            indices = np.arange(x1 + 1, current_idx + 1)
            line_vals = y1 + slope_cand * (indices - x1)
            if is_bullish:
                if np.any(closes[x1 + 1 : current_idx + 1] < line_vals - (0.15 * atr)):
                    continue
            else:
                if np.any(closes[x1 + 1 : current_idx + 1] > line_vals + (0.15 * atr)):
                    continue

            if best_slope is None:
                best_slope = slope_cand
                best_x2 = x2_cand
            elif is_bullish and slope_cand < best_slope:
                best_slope = slope_cand
                best_x2 = x2_cand
            elif (not is_bullish) and slope_cand > best_slope:
                best_slope = slope_cand
                best_x2 = x2_cand

        if best_x2 is not None and best_slope is not None:
            end_level = float(y1 + best_slope * (current_idx - x1))
            trendline_data = {
                "start_time":  int(dates[x1]),
                "start_level": float(y1),
                "end_time":    int(dates[current_idx]),
                "end_level":   end_level,
                "start_idx":   int(x1),
                "slope":       float(best_slope),
                "type":        "Demand Trendline" if is_bullish else "Supply Trendline",
                "color":       "rgba(59, 255, 130, 0.85)" if is_bullish else "rgba(255, 59, 59, 0.85)",
            }

    poi_level = active_ob["top"] if is_bullish else active_ob["bottom"]
    live_close = float(closes[-1])

    # ── 4. True Pullback Liquidity / Inducement (Only AFTER Departure!) ─────
    liq_data = find_pullback_liquidity(
        df, is_bullish, depart_idx, active_ob["top"], active_ob["bottom"], atr, decimals
    )

    targets = find_structural_targets(
        df, raw_highs, raw_lows, is_bullish, poi_level, live_close, int(anchor_idx), anchor_price, atr, decimals
    )

    # ── 5. Build Clean Chart Lines: Peak Formation + 1 Active BOS/CHoCH + Targets ──
    all_lines = [
        {
            "level":      round(anchor_price, decimals),
            "start_time": anchor_time,
            "end_time":   int(dates[-1]),
            "type":       pattern_name,
            "color":      anchor_color,
            "is_anchor":  True,
            "is_struct":  False,
        },
        {
            "level":      break_level,
            "start_time": break_start_time,
            "end_time":   break_end_time,
            "type":       f"{break_kind} {'▲' if is_bullish else '▼'} ({break_level:.{decimals}f})",
            "color":      "rgba(0, 230, 118, 0.90)" if is_bullish else "rgba(255, 82, 82, 0.90)",
            "is_anchor":  False,
            "is_struct":  True,
        }
    ]

    if liq_data.get("liq_line"):
        all_lines.append(liq_data["liq_line"])

    target_line_color = "rgba(255, 193, 7, 0.95)"
    for t in targets:
        all_lines.append({
            "level":      t["level"],
            "start_time": t["start_time"],
            "end_time":   t["end_time"],
            "type":       t["label"],
            "color":      target_line_color,
            "is_anchor":  False,
            "is_struct":  False,
        })

    step_seconds = int(dates[-1] - dates[-2]) if len(dates) >= 2 else 3600
    projection_path = [
        {"time": int(dates[-1]), "price": round(live_close, decimals), "label": "Now"},
        {"time": int(dates[-1] + step_seconds * 4), "price": round(poi_level, decimals), "label": "POI Entry Zone"},
        {"time": int(dates[-1] + step_seconds * 16), "price": targets[-1]["level"], "label": targets[-1]["label"]},
    ]

    sweeps = detect_liquidity_sweeps(df, raw_highs, raw_lows, atr)

    return {
        "cycle":           cycle,
        "is_bullish":      is_bullish,
        "break_kind":      break_kind,
        "break_level":     break_level,
        "break_idx":       int(break_idx),
        "depart_idx":      int(depart_idx),
        "poi_idx":         int(poi_idx),
        "has_fvg":         has_fvg,
        "liq_data":        liq_data,
        "lines":           all_lines,
        "anchor":          anchor_price,
        "anchor_idx":      int(anchor_idx),
        "atr":             atr,
        "peak_confirmed":  peak_confirmed,
        "sweeps":          sweeps,
        "swing_order":     swing_order,
        "raw_highs":       raw_highs.tolist(),
        "raw_lows":        raw_lows.tolist(),
        "trendline":       trendline_data,
        "zone_boxes":      zone_boxes,
        "poi_level":       round(poi_level, decimals),
        "targets":         targets,
        "projection_path": projection_path,
    }


def evaluate_entry_signal(df: pd.DataFrame, full_df: pd.DataFrame, ms: Dict, current_price: float,
                          ema_50: float, ema_200: float, rsi: float,
                          htf_aligned: bool, decimals: int, pip_size: float,
                          state_key: Optional[str] = None) -> Dict:
    is_bullish     = ms["is_bullish"]
    break_kind     = ms.get("break_kind", "CHoCH")
    break_level    = ms.get("break_level", ms["anchor"])
    depart_idx     = ms.get("depart_idx", ms["anchor_idx"] + 1)
    has_fvg        = ms.get("has_fvg", False)
    liq_data       = ms.get("liq_data", {})
    peak_confirmed = ms["peak_confirmed"]
    anchor_price   = ms["anchor"]
    anchor_idx     = ms["anchor_idx"]
    atr            = ms["atr"]
    trendline      = ms["trendline"]
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

    has_liquidity = bool(liq_data.get("has_liquidity", False))
    liq_swept     = bool(liq_data.get("swept", False))
    liq_type      = liq_data.get("liq_type", "Inducement (IDM)")
    liq_level     = liq_data.get("liq_level")

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

    # ── 2. SCAN FOR A DETERMINISTIC, UNCLOSED 1H SIGNAL CANDLE AFTER DEPARTURE ──
    locked_trade = ACTIVE_TRADES.get(state_key) if state_key else None

    if locked_trade is None and peak_confirmed and total > depart_idx + 1:
        search_start = max(depart_idx + 1, total - 35)
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
                touched_ob = (min(c_low, p_low) <= active_ob["top"] + 0.12 * atr) and (c_close >= active_ob["bottom"])
                touched_tl = False
                tl_k = None
                if trendline and k > trendline["start_idx"] + 2:
                    tl_k = trendline["start_level"] + trendline["slope"] * (k - trendline["start_idx"])
                    touched_tl = (min(c_low, p_low) <= tl_k + 0.18 * atr) and (c_close >= tl_k - 0.05 * atr)

                if not (touched_ob or touched_tl):
                    continue

                zone_ref = tl_k if (touched_tl and tl_k is not None) else active_ob["top"]
                is_bull_rej = (
                    ((c_close > c_open) and (body_sz >= 0.10 * atr))
                    or (l_wick >= 0.18 * atr and c_close >= c_low + 0.55 * max(c_high - c_low, 1e-9))
                )
                not_chasing = (c_close - zone_ref) <= (0.60 * atr)
                if not (is_bull_rej and not_chasing):
                    continue

                entry_k = round(c_close, decimals)
                floor_k = min(active_ob["bottom"], c_low, p_low) if touched_ob else min(tl_k, c_low, p_low)
                sl_k    = round(min(floor_k - 0.28 * atr, entry_k - 0.45 * atr), decimals)
                risk_k  = max(abs(entry_k - sl_k), 1e-6)

                t_list  = find_structural_targets(df.iloc[:k + 1], ms["raw_highs"], ms["raw_lows"], True, entry_k, entry_k, anchor_idx, anchor_price, atr, decimals)
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
                    "zone_name":   "Origin Buy POI + FVG" if has_fvg else "Buy Zone / POI",
                }
                if state_key:
                    ACTIVE_TRADES[state_key] = locked_trade
                break

            else:
                touched_ob = (max(c_high, p_high) >= active_ob["bottom"] - 0.12 * atr) and (c_close <= active_ob["top"])
                touched_tl = False
                tl_k = None
                if trendline and k > trendline["start_idx"] + 2:
                    tl_k = trendline["start_level"] + trendline["slope"] * (k - trendline["start_idx"])
                    touched_tl = (max(c_high, p_high) >= tl_k - 0.18 * atr) and (c_close <= tl_k + 0.05 * atr)

                if not (touched_ob or touched_tl):
                    continue

                zone_ref = tl_k if (touched_tl and tl_k is not None) else active_ob["bottom"]
                is_bear_rej = (
                    ((c_close < c_open) and (body_sz >= 0.10 * atr))
                    or (u_wick >= 0.18 * atr and c_close <= c_high - 0.55 * max(c_high - c_low, 1e-9))
                )
                not_chasing = (zone_ref - c_close) <= (0.60 * atr)
                if not (is_bear_rej and not_chasing):
                    continue

                entry_k = round(c_close, decimals)
                ceil_k  = max(active_ob["top"], c_high, p_high) if touched_ob else max(tl_k, c_high, p_high)
                sl_k    = round(max(ceil_k + 0.28 * atr, entry_k + 0.45 * atr), decimals)
                risk_k  = max(abs(sl_k - entry_k), 1e-6)

                t_list  = find_structural_targets(df.iloc[:k + 1], ms["raw_highs"], ms["raw_lows"], False, entry_k, entry_k, anchor_idx, anchor_price, atr, decimals)
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
                    "zone_name":   "Origin Sell POI + FVG" if has_fvg else "Sell Zone / POI",
                }
                if state_key:
                    ACTIVE_TRADES[state_key] = locked_trade
                break

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
            {"step": f"1. Peak {'Low (W)' if is_bullish else 'High (M)'} & {break_kind} {'▲' if is_bullish else '▼'}", "done": True, "detail": f"Cleared {break_level:.{decimals}f} ✅"},
            {"step": "2. Origin POI + Fair Value Gap (FVG)", "done": True, "detail": f"{active_ob['bottom']:.{decimals}f}–{active_ob['top']:.{decimals}f} ✅"},
            {"step": f"3. {liq_type} Swept into POI", "done": True, "detail": f"Tapped @ {f_entry:.{decimals}f} ✅"},
            {"step": f"4. 1H Rejection Close (RR 1:{f_rr})", "done": True, "detail": "Confirmed ✅"},
        ]

        active_conf = 82 + (4 if has_fvg else 0) + (4 if has_liquidity else 0) + (4 if htf_aligned else 0)

        trigger_note = (
            f"{'🟢 BUY' if is_bullish else '🔴 SELL'} EXECUTING: Locked Entry @ {f_entry:.{decimals}f} ({z_name}) | "
            f"Target 1: {f_tp1:.{decimals}f} | Target 2: {f_tp2:.{decimals}f} | SL: {f_sl:.{decimals}f}"
        )

        return {
            "signal":     f_dir,
            "confidence": min(96, active_conf),
            "setup_name": f"SMC {z_name} Sniper",
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

    # ── 3. STRICT POI TAP CHECK (Must Be AFTER Departure & Near POI Now!) ───
    live_high = max(float(full_df['High'].iloc[-1]), current_price)
    live_low  = min(float(full_df['Low'].iloc[-1]), current_price)
    live_open = float(full_df['Open'].iloc[-1])
    full_len  = len(full_df)

    # Price can only tap the POI on a pullback bar AFTER the departure bar AND when current_price is actually near the POI!
    has_departed = (full_len - 1) > depart_idx

    if is_bullish:
        in_zone_now = (
            has_departed
            and (live_low <= poi_level + 0.12 * atr)
            and (current_price >= active_ob["bottom"] - 0.08 * atr)
            and ((current_price - poi_level) <= 0.55 * atr)
        )
        planned_sl = round(min(active_ob["bottom"], poi_level) - (0.30 * atr), decimals)
        risk       = max(abs(poi_level - planned_sl), 1e-6)
        best_rr    = round(max(abs(t1_level - poi_level), abs(t2_level - poi_level)) / risk, 2)

        liq_detail = (
            f"Swept {liq_level:.{decimals}f} ✅" if (has_liquidity and (liq_swept or in_zone_now))
            else f"Sweep {liq_level:.{decimals}f} first" if has_liquidity
            else "Awaiting Pullback"
        )

        checklist = [
            {"step": f"1. Peak Low (W) & {break_kind} ▲", "done": bool(peak_confirmed), "detail": f"Cleared {break_level:.{decimals}f}"},
            {"step": "2. Origin Buy POI + FVG", "done": bool(has_fvg), "detail": f"{poi_level:.{decimals}f}" + (" (FVG ✅)" if has_fvg else " (OB Valid)")},
            {"step": f"3. {liq_type}", "done": bool((has_liquidity and liq_swept) or in_zone_now), "detail": liq_detail},
            {"step": f"4. 1H Rejection Close (RR 1:{best_rr})", "done": False, "detail": "Watch 1H Close!" if in_zone_now else f"Wait for {poi_level:.{decimals}f}"},
        ]

        if in_zone_now:
            stage = "IN_ZONE_CONFIRMING"
            trig_lvl = round(max(live_open, poi_level), decimals)
            note = f"🎯 PRICE TAPPED BUY POI ({poi_level:.{decimals}f})! Wait for the current 1H candle to close GREEN above {trig_lvl:.{decimals}f} to lock BUY."
        else:
            stage = "AWAITING_PULLBACK"
            pips_away = round(abs(current_price - poi_level) / max(pip_size, 1e-6), 1)
            note = f"⏳ NO TRADE YET: Price is {pips_away} pips above Buy POI ({poi_level:.{decimals}f}). Wait for pullback into zone."

        return {
            "signal":     "WAIT",
            "confidence": 68 if in_zone_now else 52,
            "setup_name": "SMC Bullish Sniper — " + ("In Buy POI" if in_zone_now else "Awaiting Pullback"),
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
        in_zone_now = (
            has_departed
            and (live_high >= poi_level - 0.12 * atr)
            and (current_price <= active_ob["top"] + 0.08 * atr)
            and ((poi_level - current_price) <= 0.55 * atr)
        )
        planned_sl = round(max(active_ob["top"], poi_level) + (0.30 * atr), decimals)
        risk       = max(abs(planned_sl - poi_level), 1e-6)
        best_rr    = round(max(abs(poi_level - t1_level), abs(poi_level - t2_level)) / risk, 2)

        liq_detail = (
            f"Swept {liq_level:.{decimals}f} ✅" if (has_liquidity and (liq_swept or in_zone_now))
            else f"Sweep {liq_level:.{decimals}f} first" if has_liquidity
            else "Awaiting Rally"
        )

        checklist = [
            {"step": f"1. Peak High (M) & {break_kind} ▼", "done": bool(peak_confirmed), "detail": f"Cleared {break_level:.{decimals}f}"},
            {"step": "2. Origin Sell POI + FVG", "done": bool(has_fvg), "detail": f"{poi_level:.{decimals}f}" + (" (FVG ✅)" if has_fvg else " (OB Valid)")},
            {"step": f"3. {liq_type}", "done": bool((has_liquidity and liq_swept) or in_zone_now), "detail": liq_detail},
            {"step": f"4. 1H Rejection Close (RR 1:{best_rr})", "done": False, "detail": "Watch 1H Close!" if in_zone_now else f"Wait for {poi_level:.{decimals}f}"},
        ]

        if in_zone_now:
            stage = "IN_ZONE_CONFIRMING"
            trig_lvl = round(min(live_open, poi_level), decimals)
            note = f"🎯 PRICE TAPPED SELL POI ({poi_level:.{decimals}f})! Wait for the current 1H candle to close RED below {trig_lvl:.{decimals}f} to lock SELL."
        else:
            stage = "AWAITING_PULLBACK"
            pips_away = round(abs(poi_level - current_price) / max(pip_size, 1e-6), 1)
            note = f"⏳ NO TRADE YET: Price is {pips_away} pips below Sell POI ({poi_level:.{decimals}f}). Do not short in mid-air."

        return {
            "signal":     "WAIT",
            "confidence": 68 if in_zone_now else 52,
            "setup_name": "SMC Bearish Sniper — " + ("In Sell POI" if in_zone_now else "Awaiting Rally"),
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
            current_price=current_price,
            state_key=state_key,
            drop_forming=False,
        )
        cycle          = ms['cycle']
        lines          = ms['lines']
        sweeps         = ms['sweeps']
        peak_confirmed = ms.get('peak_confirmed', False)
        trendline_data = ms.get('trendline')
        zone_boxes     = ms.get('zone_boxes', [])
        targets        = ms.get('targets', [])

        bias_str = 'BULLISH' if ms['is_bullish'] else 'BEARISH'

        htf_aligned = False
        htf_bias_str = "NEUTRAL"
        if req.htf_candles and len(req.htf_candles) > 50:
            htf_df = process_live_candles(req.htf_candles)
            if htf_df is not None:
                htf_ema50  = safe_float(EMAIndicator(close=htf_df['Close'], window=50).ema_indicator().iloc[-1])
                htf_ema200 = safe_float(EMAIndicator(close=htf_df['Close'], window=200).ema_indicator().iloc[-1])
                htf_bias_str = "BULLISH" if htf_ema50 > htf_ema200 else "BEARISH"
                htf_aligned  = (htf_bias_str == bias_str)
        else:
            htf_aligned  = (ms['is_bullish'] and ema_50 > ema_200) or ((not ms['is_bullish']) and ema_50 < ema_200)
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
                {"tf": "1H", "bias": bias_str, "strength": 85 if peak_confirmed else 60},
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
        ms = analyze_market_structure(df, profile, current_price=current_price)
        return {
            "✅ ENGINE VERSION": "AuraBrain v7.2 (Clean Peak + SMC 4-Confluence Sniper)",
            "📊 INSTRUMENT":     req.currency,
            "🎯 STRUCTURE":      ms['cycle'],
            "⚓ PEAK ANCHOR":    ms['anchor'],
            "📦 POI ZONE":       ms['zone_boxes'],
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