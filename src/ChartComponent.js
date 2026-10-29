import React, { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, ColorType, CandlestickSeries } from 'lightweight-charts';
import axios from 'axios';
import io from 'socket.io-client';

const API_URL = 'https://aura-trade-v1.onrender.com';

const SYMBOL_DISPLAY_NAMES = {
  'GC=F':     'XAU/USD',
  'XAUUSD':   'XAU/USD',
  'EURUSD=X': 'EUR/USD',
  'EURUSD':   'EUR/USD',
  'GBPUSD=X': 'GBP/USD',
  'GBPUSD':   'GBP/USD',
  'JPY=X':    'USD/JPY',
  'USDJPY':   'USD/JPY',
  'AUDUSD=X': 'AUD/USD',
  'AUDUSD':   'AUD/USD',
  'CAD=X':    'USD/CAD',
  'USDCAD':   'USD/CAD',
  'CHF=X':    'USD/CHF',
  'USDCHF':   'USD/CHF',
  'NZDUSD=X': 'NZD/USD',
  'NZDUSD':   'NZD/USD',
};

const formatSymbolDisplay = (sym = '') => {
  const clean = sym.toUpperCase().trim();
  return SYMBOL_DISPLAY_NAMES[clean] || clean.replace('=X', '');
};

const getPriceFormat = (sym = '') => {
  const s = symbolToUpper(sym);
  if (s.includes('GC=F') || s.includes('XAU') || s.includes('GOLD')) {
    return { type: 'price', precision: 2, minMove: 0.01 };
  }
  if (s.includes('JPY')) {
    return { type: 'price', precision: 3, minMove: 0.001 };
  }
  return { type: 'price', precision: 5, minMove: 0.00001 };
};

const symbolToUpper = (sym = '') => String(sym || '').toUpperCase();

const safeTimeToX = (timeScale, candles, targetTime) => {
  if (!timeScale || targetTime == null) return null;
  const direct = timeScale.timeToCoordinate(targetTime);
  if (direct !== null) return direct;
  if (!candles || !candles.length) return null;

  const lastTime  = candles[candles.length - 1].time;
  const firstTime = candles[0].time;
  if (targetTime >= lastTime) return timeScale.timeToCoordinate(lastTime);
  if (targetTime <= firstTime) return timeScale.timeToCoordinate(firstTime);

  let lo = 0, hi = candles.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (candles[mid].time === targetTime) return timeScale.timeToCoordinate(candles[mid].time);
    if (candles[mid].time < targetTime) lo = mid + 1;
    else hi = mid - 1;
  }
  const idxLo = Math.min(candles.length - 1, Math.max(0, lo));
  const idxHi = Math.min(candles.length - 1, Math.max(0, hi));
  const bestIdx = Math.abs(candles[idxHi].time - targetTime) <= Math.abs(candles[idxLo].time - targetTime)
    ? idxHi
    : idxLo;
  return timeScale.timeToCoordinate(candles[bestIdx].time);
};

// Local fallback TDI calculator so the TDI panel renders immediately even before Python responds
const computeLocalTDI = (candles) => {
  if (!candles || candles.length < 20) return [];
  const closes = candles.map(c => c.close);
  const n = closes.length;
  const period = 13;

  const rsi = new Array(n).fill(50);
  let avgGain = 0;
  let avgLoss = 0;

  for (let i = 1; i < n; i++) {
    const chg = closes[i] - closes[i - 1];
    const gain = chg > 0 ? chg : 0;
    const loss = chg < 0 ? -chg : 0;

    if (i <= period) {
      avgGain += gain / period;
      avgLoss += loss / period;
      if (i === period) {
        const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
        rsi[i] = 100 - 100 / (1 + rs);
      }
    } else {
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
      const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
      rsi[i] = 100 - 100 / (1 + rs);
    }
  }

  const raw = [];
  for (let i = 0; i < n; i++) {
    const gStart = Math.max(0, i - 1);
    let gSum = 0;
    for (let j = gStart; j <= i; j++) gSum += rsi[j];
    const green = gSum / (i - gStart + 1);

    const rStart = Math.max(0, i - 6);
    let rSum = 0;
    for (let j = rStart; j <= i; j++) rSum += rsi[j];
    const red = rSum / (i - rStart + 1);

    const bStart = Math.max(0, i - 33);
    const bLen = i - bStart + 1;
    let bSum = 0;
    for (let j = bStart; j <= i; j++) bSum += rsi[j];
    const yellow = bSum / bLen;

    let varSum = 0;
    for (let j = bStart; j <= i; j++) {
      const d = rsi[j] - yellow;
      varSum += d * d;
    }
    const std = Math.sqrt(varSum / Math.max(1, bLen));
    const upper = Math.min(100, Math.max(0, yellow + 1.6185 * std));
    const lower = Math.min(100, Math.max(0, yellow - 1.6185 * std));

    raw.push({
      time: candles[i].time,
      green,
      red,
      yellow,
      upper,
      lower,
      fin_type: null,
    });
  }

  // Mark ONLY clean single-peak Sharkfin apexes (no M/W multi-dot clusters)
  for (let i = 5; i < n - 2; i++) {
    const cur = raw[i];
    const prev = raw[i - 1];
    const next = raw[i + 1];

    // Check Bullish single-trough Sharkfin
    if (cur.green <= cur.lower + 1.8 && cur.green < prev.green - 0.6 && next.green > cur.green + 1.2) {
      let outsideBars = 0;
      let troughs = 0;
      for (let k = Math.max(1, i - 5); k <= Math.min(n - 2, i + 2); k++) {
        if (raw[k].green < raw[k].lower - 0.2) outsideBars++;
        if (raw[k].green <= raw[k - 1].green + 0.15 && raw[k].green <= raw[k + 1].green + 0.15 && raw[k].green <= raw[k].lower + 3.2) {
          troughs++;
        }
      }
      if (outsideBars <= 2 && troughs === 1) {
        cur.fin_type = 'BULL';
      }
    }

    // Check Bearish single-peak Sharkfin
    if (cur.green >= cur.upper - 1.8 && cur.green > prev.green + 0.6 && next.green < cur.green - 1.2) {
      let outsideBars = 0;
      let peaks = 0;
      for (let k = Math.max(1, i - 5); k <= Math.min(n - 2, i + 2); k++) {
        if (raw[k].green > raw[k].upper + 0.2) outsideBars++;
        if (raw[k].green >= raw[k - 1].green - 0.15 && raw[k].green >= raw[k + 1].green - 0.15 && raw[k].green >= raw[k].upper - 3.2) {
          peaks++;
        }
      }
      if (outsideBars <= 2 && peaks === 1) {
        cur.fin_type = 'BEAR';
      }
    }
  }

  return raw.slice(Math.max(0, n - 500));
};

class LineRenderer {
  constructor(data) { this._data = data; }

  draw(target) {
    target.useBitmapCoordinateSpace((scope) => {
      if (!this._data || this._data.length === 0) return;
      const ctx       = scope.context;
      const hPR       = scope.horizontalPixelRatio;
      const vPR       = scope.verticalPixelRatio;
      const rightEdge = scope.mediaSize.width * hPR;

      this._data.forEach((line) => {
        if (line.x1 === null || line.x2 === null || line.y === null) return;
        const x1 = Math.max(0, Math.round(line.x1 * hPR));
        const x2 = line.isStruct
          ? Math.min(Math.max(x1 + 26 * hPR, Math.round(line.x2 * hPR)), rightEdge)
          : Math.min(Math.round(line.x2 * hPR), rightEdge);
        const y  = Math.round(line.y * vPR);
        if (x1 >= rightEdge) return;

        ctx.save();
        ctx.beginPath();
        ctx.moveTo(x1, y);
        ctx.lineTo(x2, y);
        ctx.strokeStyle = line.color;
        ctx.lineWidth   = line.isAnchor ? 2.2 * hPR : 1.6 * hPR;
        ctx.setLineDash(line.isAnchor ? [] : [6 * hPR, 4 * hPR]);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.font      = `bold ${11 * hPR}px monospace`;
        ctx.fillStyle = line.color;

        if (line.isStruct) {
          const midX = Math.min(Math.max(x1 + 6 * hPR, (x1 + x2) / 2 - 38 * hPR), rightEdge - 160 * hPR);
          ctx.fillText(line.label, midX, y - 6 * vPR);
        } else {
          const labelX = Math.min(x2 + 8 * hPR, rightEdge - 165 * hPR);
          ctx.fillText(line.label, labelX, y - 6 * vPR);
        }
        ctx.restore();
      });
    });
  }
}

class LinePaneView {
  constructor(source) { this._source = source; }
  renderer() { return new LineRenderer(this._source._rendererData); }
}

class LinePrimitive {
  constructor() {
    this._rendererData = [];
    this._paneViews    = [new LinePaneView(this)];
  }

  setData(lines, series, timeScale, candles) {
    if (!lines || !Array.isArray(lines) || !candles || !candles.length) {
      this._rendererData = [];
      return;
    }
    this._rendererData = lines.map((line) => {
      if (!line.start_time || !line.end_time || line.level == null) return null;
      try {
        const x1 = safeTimeToX(timeScale, candles, line.start_time);
        const x2 = safeTimeToX(timeScale, candles, line.end_time);
        const y  = series.priceToCoordinate(line.level);
        if (x1 === null || x2 === null || y === null) return null;

        const isAnchor = Boolean(line.is_anchor || line.type?.includes('Peak'));
        const isStruct = Boolean(line.is_struct || line.type?.includes('BOS') || line.type?.includes('CHoCH'));
        const color    = line.color || (isAnchor ? 'rgba(255,215,0,0.9)' : 'rgba(239,83,80,0.8)');

        return { x1, x2, y, color, label: line.type || 'LEVEL', isAnchor, isStruct };
      } catch { return null; }
    }).filter(Boolean);
  }

  paneViews() { return this._paneViews; }
}

class TrendlineRenderer {
  constructor(data) { this._data = data; }

  draw(target) {
    target.useBitmapCoordinateSpace((scope) => {
      if (!this._data) return;
      const ctx       = scope.context;
      const hPR       = scope.horizontalPixelRatio;
      const vPR       = scope.verticalPixelRatio;
      const rightEdge = scope.mediaSize.width * hPR;

      const { x1, y1, x2, y2, color, label } = this._data;
      if (x1 === null || y1 === null || x2 === null || y2 === null) return;

      const px1 = Math.max(0, Math.round(x1 * hPR)), py1 = Math.round(y1 * vPR);
      const px2 = Math.min(Math.round(x2 * hPR), rightEdge), py2 = Math.round(y2 * vPR);
      if (px1 >= rightEdge) return;

      ctx.beginPath();
      ctx.moveTo(px1, py1);
      ctx.lineTo(px2, py2);
      ctx.strokeStyle = color;
      ctx.lineWidth   = 2 * hPR;
      ctx.stroke();

      ctx.font      = `bold ${11 * hPR}px monospace`;
      ctx.fillStyle = color;
      ctx.fillText(label, Math.min(px2 + 6 * hPR, rightEdge - 125 * hPR), py2 - 6 * vPR);
    });
  }
}

class TrendlinePaneView {
  constructor(source) { this._source = source; }
  renderer() { return new TrendlineRenderer(this._source._rendererData); }
}

class TrendlinePrimitive {
  constructor() {
    this._rendererData = null;
    this._paneViews    = [new TrendlinePaneView(this)];
  }

  setData(tl, series, timeScale, candles) {
    if (!tl || !tl.start_time || !tl.end_time || tl.start_level == null || tl.end_level == null || !candles || !candles.length) {
      this._rendererData = null;
      return;
    }
    try {
      const x1 = safeTimeToX(timeScale, candles, tl.start_time);
      const x2 = safeTimeToX(timeScale, candles, tl.end_time);
      const y1 = series.priceToCoordinate(tl.start_level);
      const y2 = series.priceToCoordinate(tl.end_level);
      if (x1 === null || x2 === null || y1 === null || y2 === null) {
        this._rendererData = null;
        return;
      }
      this._rendererData = { x1, y1, x2, y2, color: tl.color || 'rgba(255,215,0,0.8)', label: tl.type || 'Trendline' };
    } catch { this._rendererData = null; }
  }

  paneViews() { return this._paneViews; }
}

class OBZoneRenderer {
  constructor(data) { this._data = data; }

  draw(target) {
    target.useBitmapCoordinateSpace((scope) => {
      if (!this._data || this._data.length === 0) return;
      const ctx       = scope.context;
      const hPR       = scope.horizontalPixelRatio;
      const vPR       = scope.verticalPixelRatio;
      const rightEdge = scope.mediaSize.width * hPR;

      this._data.forEach((zone) => {
        if (zone.x1 === null || zone.yTop === null || zone.yBottom === null) return;
        const x1      = Math.max(0, Math.round(zone.x1 * hPR));
        const x2      = zone.x2 !== null ? Math.min(Math.round(zone.x2 * hPR), rightEdge) : rightEdge;
        const yTop    = Math.round(Math.min(zone.yTop, zone.yBottom) * vPR);
        const yBottom = Math.round(Math.max(zone.yTop, zone.yBottom) * vPR);
        const boxH    = Math.max(4 * vPR, yBottom - yTop);
        if (x1 >= rightEdge) return;

        ctx.save();
        ctx.fillStyle   = zone.isBullish ? 'rgba(38,166,154,0.13)' : 'rgba(239,83,80,0.13)';
        ctx.fillRect(x1, yTop, Math.max(12 * hPR, x2 - x1), boxH);
        ctx.strokeStyle = zone.isBullish ? 'rgba(38,166,154,0.80)' : 'rgba(239,83,80,0.80)';
        ctx.lineWidth   = 1.2 * hPR;
        ctx.strokeRect(x1, yTop, Math.max(12 * hPR, x2 - x1), boxH);

        ctx.font      = `bold ${10 * hPR}px monospace`;
        ctx.fillStyle = zone.isBullish ? 'rgba(38,166,154,0.98)' : 'rgba(239,83,80,0.98)';
        const textX   = Math.min(x1 + 6 * hPR, rightEdge - 240 * hPR);
        ctx.fillText(zone.label || 'ORDER BLOCK', Math.max(4 * hPR, textX), yTop + 13 * vPR);
        ctx.restore();
      });
    });
  }
}

class OBZonePaneView {
  constructor(source) { this._source = source; }
  renderer() { return new OBZoneRenderer(this._source._rendererData); }
}

class OBZonePrimitive {
  constructor() {
    this._rendererData = [];
    this._paneViews    = [new OBZonePaneView(this)];
  }

  setData(obs, series, timeScale, candles) {
    if (!obs || !Array.isArray(obs) || !candles || !candles.length) {
      this._rendererData = [];
      return;
    }
    this._rendererData = obs.map((ob) => {
      if (!ob.time || ob.top == null || ob.bottom == null) return null;
      try {
        const x1      = safeTimeToX(timeScale, candles, ob.time);
        const x2      = ob.end_time ? safeTimeToX(timeScale, candles, ob.end_time) : null;
        const yTop    = series.priceToCoordinate(ob.top);
        const yBottom = series.priceToCoordinate(ob.bottom);
        if (x1 === null || yTop === null || yBottom === null) return null;
        return {
          x1,
          x2,
          yTop,
          yBottom,
          isBullish: ob.type?.includes('BULL'),
          label:     ob.label || 'ORDER BLOCK',
        };
      } catch { return null; }
    }).filter(Boolean);
  }

  paneViews() { return this._paneViews; }
}

class ProjectionRenderer {
  constructor(data) { this._data = data; }

  draw(target) {
    target.useBitmapCoordinateSpace((scope) => {
      if (!this._data || !this._data.points || this._data.points.length < 3) return;
      const ctx       = scope.context;
      const hPR       = scope.horizontalPixelRatio;
      const vPR       = scope.verticalPixelRatio;
      const rightEdge = scope.mediaSize.width * hPR;

      const [p0, p1, p2] = this._data.points;
      if (p0.x == null || p0.y == null || p1.y == null || p2.y == null) return;

      const x0 = Math.round(p0.x * hPR);
      const y0 = Math.round(p0.y * vPR);
      const x1 = Math.min(x0 + 28 * hPR, rightEdge - 65 * hPR);
      const y1 = Math.round(p1.y * vPR);
      const x2 = Math.min(x0 + 78 * hPR, rightEdge - 20 * hPR);
      const y2 = Math.round(p2.y * vPR);

      if (x0 >= rightEdge - 30 * hPR) return;

      const color = this._data.color || 'rgba(255, 215, 0, 0.9)';

      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.strokeStyle = color;
      ctx.lineWidth   = 2 * hPR;
      ctx.setLineDash([4 * hPR, 3 * hPR]);
      ctx.stroke();
      ctx.setLineDash([]);

      const angle   = Math.atan2(y2 - y1, x2 - x1);
      const headLen = 9 * hPR;
      ctx.beginPath();
      ctx.moveTo(x2, y2);
      ctx.lineTo(x2 - headLen * Math.cos(angle - Math.PI / 6), y2 - headLen * Math.sin(angle - Math.PI / 6));
      ctx.moveTo(x2, y2);
      ctx.lineTo(x2 - headLen * Math.cos(angle + Math.PI / 6), y2 - headLen * Math.sin(angle + Math.PI / 6));
      ctx.strokeStyle = color;
      ctx.lineWidth   = 2 * hPR;
      ctx.stroke();
      ctx.restore();
    });
  }
}

class ProjectionPaneView {
  constructor(source) { this._source = source; }
  renderer() { return new ProjectionRenderer(this._source._rendererData); }
}

class ProjectionPrimitive {
  constructor() {
    this._rendererData = null;
    this._paneViews    = [new ProjectionPaneView(this)];
  }

  setData(path, series, timeScale, candles) {
    if (!path || !Array.isArray(path) || path.length < 3 || !candles || !candles.length) {
      this._rendererData = null;
      return;
    }
    try {
      const x0 = safeTimeToX(timeScale, candles, path[0].time);
      const y0 = series.priceToCoordinate(path[0].price);
      const y1 = series.priceToCoordinate(path[1].price);
      const y2 = series.priceToCoordinate(path[2].price);
      if (x0 === null || y0 === null || y1 === null || y2 === null) {
        this._rendererData = null;
        return;
      }
      const isBull = path[2].price > path[1].price;
      this._rendererData = {
        points: [
          { x: x0, y: y0 },
          { x: x0, y: y1 },
          { x: x0, y: y2 },
        ],
        color: isBull ? 'rgba(59, 255, 130, 0.9)' : 'rgba(255, 82, 82, 0.9)',
      };
    } catch { this._rendererData = null; }
  }

  paneViews() { return this._paneViews; }
}

// ============================================================================
// 🦈 TDI (TRADERS DYNAMIC INDEX) SUB-WINDOW PRIMITIVE (CLEAN SINGLE-FIN DOT)
// ============================================================================
class TDIPaneRenderer {
  constructor(data) { this._data = data; }

  draw(target) {
    target.useBitmapCoordinateSpace((scope) => {
      if (!this._data || !this._data.visible || !this._data.points || this._data.points.length < 2) return;

      const ctx   = scope.context;
      const hPR   = scope.horizontalPixelRatio;
      const vPR   = scope.verticalPixelRatio;
      const fullW = scope.mediaSize.width * hPR;
      const fullH = scope.mediaSize.height * vPR;

      const paneH   = Math.round(fullH * 0.24);
      const paneTop = fullH - paneH;
      const padTop  = 18 * vPR;
      const padBot  = 8 * vPR;
      const plotTop = paneTop + padTop;
      const plotBot = fullH - padBot;
      const plotH   = Math.max(20 * vPR, plotBot - plotTop);

      // Map RSI 12..88 range into the sub-window height
      const valToY = (v) => {
        const clamped = Math.max(12, Math.min(88, v));
        return plotBot - ((clamped - 12) / (88 - 12)) * plotH;
      };

      const pts = this._data.points.filter(p => p.x != null && p.x * hPR >= -20 && p.x * hPR <= fullW + 20);
      if (pts.length < 2) return;

      ctx.save();

      // 1. Sub-panel dark background & top divider border
      ctx.fillStyle = 'rgba(13, 17, 24, 0.94)';
      ctx.fillRect(0, paneTop, fullW, paneH);

      ctx.beginPath();
      ctx.moveTo(0, paneTop);
      ctx.lineTo(fullW, paneTop);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.14)';
      ctx.lineWidth   = 1.2 * vPR;
      ctx.stroke();

      // 2. Reference levels: 68 (Overbought), 50 (Mid), 32 (Oversold)
      [
        { lvl: 68, col: 'rgba(239, 83, 80, 0.30)' },
        { lvl: 50, col: 'rgba(255, 255, 255, 0.16)' },
        { lvl: 32, col: 'rgba(38, 166, 154, 0.30)' },
      ].forEach(({ lvl, col }) => {
        const y = Math.round(valToY(lvl));
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(fullW, y);
        ctx.strokeStyle = col;
        ctx.lineWidth   = 1 * vPR;
        ctx.setLineDash([4 * hPR, 4 * hPR]);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.font      = `${9 * hPR}px monospace`;
        ctx.fillStyle = 'rgba(156, 163, 175, 0.65)';
        ctx.fillText(String(lvl), fullW - 22 * hPR, y - 3 * vPR);
      });

      // 3. Blue Volatility Band Water Fill (between Upper & Lower Blue Bands)
      ctx.beginPath();
      pts.forEach((p, idx) => {
        const x = Math.round(p.x * hPR);
        const y = Math.round(valToY(p.upper));
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      for (let idx = pts.length - 1; idx >= 0; idx--) {
        const p = pts[idx];
        const x = Math.round(p.x * hPR);
        const y = Math.round(valToY(p.lower));
        ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.fillStyle = 'rgba(41, 98, 255, 0.08)';
      ctx.fill();

      // Helper to stroke a continuous TDI line
      const strokeTDILine = (key, color, width, dash = []) => {
        ctx.beginPath();
        pts.forEach((p, idx) => {
          const x = Math.round(p.x * hPR);
          const y = Math.round(valToY(p[key]));
          if (idx === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = color;
        ctx.lineWidth   = width * hPR;
        ctx.setLineDash(dash);
        ctx.stroke();
        ctx.setLineDash([]);
      };

      // 4. Draw Upper & Lower Blue Volatility Bands
      strokeTDILine('upper', 'rgba(41, 182, 246, 0.85)', 1.3);
      strokeTDILine('lower', 'rgba(41, 182, 246, 0.85)', 1.3);

      // 5. Draw Yellow Market Base Line (SMA 34)
      strokeTDILine('yellow', 'rgba(255, 214, 0, 0.90)', 1.4);

      // 6. Draw Red Trade Signal Line (SMA 7)
      strokeTDILine('red', 'rgba(255, 82, 82, 0.95)', 1.6);

      // 7. Draw Green RSI Price Line (SMA 2 of RSI 13)
      strokeTDILine('green', 'rgba(0, 230, 118, 0.98)', 2.0);

      // 8. Draw a single glowing dot STRICTLY on confirmed Clean Single-Peak Sharkfin tips!
      pts.forEach((p) => {
        if (p.finType !== 'BULL' && p.finType !== 'BEAR') return;

        const isBullFin = p.finType === 'BULL';
        const x = Math.round(p.x * hPR);
        const y = Math.round(valToY(p.green));
        const dotColor = isBullFin ? '#00E676' : '#FF1744';

        // Outer halo ring
        ctx.beginPath();
        ctx.arc(x, y, 6.0 * hPR, 0, Math.PI * 2);
        ctx.fillStyle = isBullFin ? 'rgba(0, 230, 118, 0.25)' : 'rgba(255, 23, 68, 0.25)';
        ctx.fill();

        // Solid inner apex dot
        ctx.beginPath();
        ctx.arc(x, y, 3.6 * hPR, 0, Math.PI * 2);
        ctx.fillStyle = dotColor;
        ctx.fill();
        ctx.strokeStyle = '#FFFFFF';
        ctx.lineWidth = 1.0 * hPR;
        ctx.stroke();
      });

      // 9. Header Legend inside TDI Sub-Window
      const last = pts[pts.length - 1];
      ctx.font      = `bold ${10 * hPR}px monospace`;
      ctx.fillStyle = '#E8EDF3';
      ctx.fillText('🦈 TDI (13, 2, 7, 34)', 12 * hPR, paneTop + 13 * vPR);

      ctx.fillStyle = '#00E676';
      ctx.fillText(`Green: ${last.green.toFixed(1)}`, 165 * hPR, paneTop + 13 * vPR);

      ctx.fillStyle = '#FF5252';
      ctx.fillText(`Red: ${last.red.toFixed(1)}`, 255 * hPR, paneTop + 13 * vPR);

      ctx.fillStyle = '#FFD600';
      ctx.fillText(`Base: ${last.yellow.toFixed(1)}`, 335 * hPR, paneTop + 13 * vPR);

      ctx.fillStyle = '#29B6F6';
      ctx.fillText(`Bands: ${last.lower.toFixed(1)}–${last.upper.toFixed(1)}`, 425 * hPR, paneTop + 13 * vPR);

      ctx.restore();
    });
  }
}

class TDIPaneView {
  constructor(source) { this._source = source; }
  renderer() { return new TDIPaneRenderer(this._source._rendererData); }
}

class TDIPanePrimitive {
  constructor() {
    this._rendererData = { visible: true, points: [] };
    this._paneViews    = [new TDIPaneView(this)];
  }

  setData(tdiSeries, timeScale, candles, visible = true) {
    if (!visible || !candles || !candles.length) {
      this._rendererData = { visible: false, points: [] };
      return;
    }
    const sourceData = (tdiSeries && tdiSeries.length > 10) ? tdiSeries : computeLocalTDI(candles);
    if (!sourceData || !sourceData.length) {
      this._rendererData = { visible: false, points: [] };
      return;
    }

    const points = sourceData.map((item) => {
      const x = safeTimeToX(timeScale, candles, item.time);
      if (x === null) return null;
      return {
        x,
        green:   Number(item.green),
        red:     Number(item.red),
        yellow:  Number(item.yellow),
        upper:   Number(item.upper),
        lower:   Number(item.lower),
        finType: item.fin_type || null,
      };
    }).filter(Boolean);

    this._rendererData = { visible: true, points };
  }

  paneViews() { return this._paneViews; }
}

const ChartComponent = ({ symbol = 'GC=F', levels, visuals, tradeSetup }) => {
  const chartContainerRef       = useRef(null);
  const chartRef                = useRef(null);
  const candleSeriesRef         = useRef(null);

  const linePrimitiveRef        = useRef(null);
  const trendlinePrimitiveRef   = useRef(null);
  const obZonePrimitiveRef      = useRef(null);
  const projectionPrimitiveRef  = useRef(null);
  const tdiPrimitiveRef         = useRef(null);
  const activeLinesRef          = useRef([]);
  const isChartReady            = useRef(false);
  const isSymbolLoadedRef       = useRef(false);
  const currentBarRef           = useRef(null);
  const timeframeRef            = useRef('1h');
  const allDataRef              = useRef([]);
  const isLoadingRef            = useRef(false);
  const latestCandleRef         = useRef(null);
  const symbolRef               = useRef(symbol);

  const [timeframe,           setTimeframe]           = useState('1h');
  const [connectionStatus,    setConnectionStatus]    = useState('Connecting...');
  const [newsData,            setNewsData]            = useState([]);
  const [isHoveringControls,  setIsHoveringControls]  = useState(false);
  const [isExpanded,          setIsExpanded]          = useState(false);
  const [showTDI,             setShowTDI]             = useState(true);
  const showTDIRef            = useRef(true);

  useEffect(() => {
    showTDIRef.current = showTDI;
    if (chartRef.current) {
      chartRef.current.priceScale('right').applyOptions({
        scaleMargins: { top: 0.12, bottom: showTDI ? 0.28 : 0.14 },
      });
    }
  }, [showTDI]);

  const clearPrimitives = useCallback(() => {
    if (linePrimitiveRef.current)       linePrimitiveRef.current.setData([], null, null, []);
    if (trendlinePrimitiveRef.current)  trendlinePrimitiveRef.current.setData(null, null, null, []);
    if (obZonePrimitiveRef.current)     obZonePrimitiveRef.current.setData([], null, null, []);
    if (projectionPrimitiveRef.current) projectionPrimitiveRef.current.setData(null, null, null, []);
    if (tdiPrimitiveRef.current)        tdiPrimitiveRef.current.setData([], null, [], false);
  }, []);

  const updateChartData = useCallback((data) => {
    if (!candleSeriesRef.current) return;
    candleSeriesRef.current.setData(data);
  }, []);

  useEffect(() => {
    if (!chartRef.current || !chartContainerRef.current) return;
    const timer = setTimeout(() => {
      if (chartRef.current && chartContainerRef.current) {
        chartRef.current.applyOptions({
          width:  chartContainerRef.current.clientWidth,
          height: isExpanded ? window.innerHeight : 540,
        });
      }
    }, 30);
    return () => clearTimeout(timer);
  }, [isExpanded]);

  useEffect(() => {
    const onKeyDown = (e) => {
      if (e.key === 'Escape' && isExpanded) setIsExpanded(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isExpanded]);

  useEffect(() => {
    symbolRef.current = symbol;
    isSymbolLoadedRef.current = false;
    clearPrimitives();
    if (candleSeriesRef.current) {
      const pf = getPriceFormat(symbol);
      candleSeriesRef.current.applyOptions({ priceFormat: pf });
      allDataRef.current = [];
      updateChartData([]);
    }
  }, [symbol, updateChartData, clearPrimitives]);

  const processCandles = useCallback((rawData) => {
    if (!Array.isArray(rawData)) return [];
    return rawData
      .filter(d => d.open != null && d.close != null && d.time != null)
      .map(d => {
        let seconds = Number(d.time);
        if (seconds > 2_000_000_000) seconds = Math.floor(seconds / 1000);
        return { time: seconds, open: parseFloat(d.open), high: parseFloat(d.high), low: parseFloat(d.low), close: parseFloat(d.close) };
      })
      .filter(d => !isNaN(d.open) && d.open > 0)
      .sort((a, b) => a.time - b.time)
      .filter((v, i, a) => a.findIndex(t => t.time === v.time) === i);
  }, []);

  useEffect(() => {
    if (!chartContainerRef.current) return;
    const chart = createChart(chartContainerRef.current, {
      layout:          { background: { type: ColorType.Solid, color: '#161A25' }, textColor: '#D9D9D9' },
      grid:            { vertLines: { color: '#2B2B43', style: 1 }, horzLines: { color: '#2B2B43', style: 1 } },
      crosshair:       { mode: 0 },
      width:           chartContainerRef.current.clientWidth,
      height:          540,
      timeScale:       { timeVisible: true, secondsVisible: false, rightOffset: 24, barSpacing: 12 },
      rightPriceScale: { scaleMargins: { top: 0.12, bottom: 0.28 }, borderVisible: false, autoScale: true },
    });

    const initialFormat = getPriceFormat(symbolRef.current);

    const series = chart.addSeries(CandlestickSeries, {
      upColor:         '#089981',
      downColor:       '#F23645',
      borderVisible:   false,
      wickUpColor:     '#089981',
      wickDownColor:   '#F23645',
      priceFormat:     initialFormat,
    });

    const lineP = new LinePrimitive();
    series.attachPrimitive(lineP);
    linePrimitiveRef.current = lineP;

    const trendlineP = new TrendlinePrimitive();
    series.attachPrimitive(trendlineP);
    trendlinePrimitiveRef.current = trendlineP;

    const obZoneP = new OBZonePrimitive();
    series.attachPrimitive(obZoneP);
    obZonePrimitiveRef.current = obZoneP;

    const projP = new ProjectionPrimitive();
    series.attachPrimitive(projP);
    projectionPrimitiveRef.current = projP;

    const tdiP = new TDIPanePrimitive();
    series.attachPrimitive(tdiP);
    tdiPrimitiveRef.current = tdiP;

    chartRef.current        = chart;
    candleSeriesRef.current = series;
    isChartReady.current    = true;

    const onResize = () => {
      if (chartContainerRef.current && chartRef.current) {
        chartRef.current.applyOptions({
          width:  chartContainerRef.current.clientWidth,
          height: chartContainerRef.current.clientHeight || 540,
        });
      }
    };
    window.addEventListener('resize', onResize);

    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (range && range.from < 5 && !isLoadingRef.current) fetchOlderHistory();
    });

    return () => {
      isChartReady.current = false;
      window.removeEventListener('resize', onResize);
      chart.remove();
      chartRef.current        = null;
      candleSeriesRef.current = null;
    };
  }, []);

  const fetchOlderHistory = async () => {
    if (isLoadingRef.current || !allDataRef.current.length) return;
    isLoadingRef.current = true;
    const oldestTime = allDataRef.current[0].time;
    try {
      const res = await axios.get(`${API_URL}/api/candles/${timeframeRef.current}`, {
        params: { symbol: symbolRef.current, limit: 500, before: oldestTime, timestamp: Date.now() },
      });
      const newData = processCandles(res.data);
      if (!newData.length) return;
      const combined = [...newData, ...allDataRef.current]
        .filter((v, i, a) => a.findIndex(t => t.time === v.time) === i)
        .sort((a, b) => a.time - b.time);
      allDataRef.current = combined;
      updateChartData(combined);
    } catch { /* silent */ }
    finally { isLoadingRef.current = false; }
  };

  useEffect(() => {
    if (!isChartReady.current) return;
    let cancelled = false;
    isSymbolLoadedRef.current = false;
    clearPrimitives();

    const load = async () => {
      try {
        const res = await axios.get(`${API_URL}/api/candles/${timeframe}`, {
          params: { symbol, limit: 1000, timestamp: Date.now() },
        });
        if (cancelled || symbol !== symbolRef.current) return;
        const data = processCandles(res.data);
        if (isChartReady.current && candleSeriesRef.current) {
          allDataRef.current = data;
          updateChartData(data);
          if (data.length) currentBarRef.current = data[data.length - 1];
          chartRef.current?.timeScale().fitContent();
          isSymbolLoadedRef.current = true;
        }
      } catch { /* silent */ }
    };
    load();
    return () => { cancelled = true; };
  }, [timeframe, symbol, processCandles, updateChartData, clearPrimitives]);

  useEffect(() => {
    let rafId;
    const loop = () => {
      if (!isChartReady.current) return;

      if (candleSeriesRef.current && latestCandleRef.current && isSymbolLoadedRef.current) {
        candleSeriesRef.current.update(latestCandleRef.current);
        currentBarRef.current   = latestCandleRef.current;
        latestCandleRef.current = null;
      }

      if (chartRef.current && candleSeriesRef.current && isSymbolLoadedRef.current && allDataRef.current.length > 0) {
        const ts      = chartRef.current.timeScale();
        const candles = allDataRef.current;

        if (linePrimitiveRef.current) {
          linePrimitiveRef.current.setData(visuals?.bos_lines || [], candleSeriesRef.current, ts, candles);
        }
        if (trendlinePrimitiveRef.current) {
          trendlinePrimitiveRef.current.setData(visuals?.trendline, candleSeriesRef.current, ts, candles);
        }
        if (obZonePrimitiveRef.current) {
          obZonePrimitiveRef.current.setData(visuals?.order_blocks || [], candleSeriesRef.current, ts, candles);
        }
        if (projectionPrimitiveRef.current) {
          projectionPrimitiveRef.current.setData(visuals?.projection_path, candleSeriesRef.current, ts, candles);
        }
        if (tdiPrimitiveRef.current) {
          tdiPrimitiveRef.current.setData(visuals?.tdi_data, ts, candles, showTDIRef.current);
        }
      }
      rafId = requestAnimationFrame(loop);
    };
    loop();
    return () => cancelAnimationFrame(rafId);
  }, [visuals]);

  useEffect(() => {
    const fetch = async () => {
      try {
        const res = await axios.get(`${API_URL}/api/news`, { params: { limit: 100 } });
        if (Array.isArray(res.data)) setNewsData(res.data);
      } catch { /* silent */ }
    };
    fetch();
  }, []);

  useEffect(() => {
    if (!newsData.length || !isChartReady.current || !candleSeriesRef.current) return;
    try {
      const markers = newsData.filter(n => n.time).map(n => {
        let s = Number(n.time);
        if (s > 2_000_000_000) s = Math.floor(s / 1000);
        return { time: s, position: 'aboveBar', color: n.impact === 'High' ? '#ef5350' : '#ffa726', shape: 'arrowDown', text: n.impact === 'High' ? `🚩 ${n.event}` : '', size: n.impact === 'High' ? 2 : 1 };
      }).sort((a, b) => a.time - b.time);
      if (typeof candleSeriesRef.current.setMarkers === 'function')
        candleSeriesRef.current.setMarkers(markers);
    } catch { /* silent */ }
  }, [newsData]);

  useEffect(() => {
    if (!candleSeriesRef.current || !isChartReady.current) return;
    activeLinesRef.current.forEach(l => { try { candleSeriesRef.current.removePriceLine(l); } catch { /* silent */ } });
    activeLinesRef.current = [];

    const addLine = (price, color, title, style = 0, width = 2) => {
      if (price == null || isNaN(parseFloat(price))) return;
      const l = candleSeriesRef.current.createPriceLine({ price: parseFloat(price), color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title });
      activeLinesRef.current.push(l);
    };

    if (tradeSetup) {
      addLine(tradeSetup.take_profit, '#00E676', 'TP 🎯', 0, 2);
      const isBE = Boolean(tradeSetup.breakeven_active);
      addLine(
        tradeSetup.stop_loss,
        isBE ? '#FFD600' : '#FF1744',
        isBE ? 'SL (BE) 🛡️' : 'SL 🛑',
        0,
        2
      );
      addLine(tradeSetup.entry, '#2962FF', 'ENTRY 🔵', 3, 2);
    }
  }, [levels, tradeSetup]);

  useEffect(() => {
    const socket = io(API_URL, { transports: ['polling'], path: '/socket.io/' });
    socket.on('connect',    () => setConnectionStatus('Connected'));
    socket.on('disconnect', () => setConnectionStatus('Disconnected'));
    socket.on('price-update', (data) => {
      if (data.symbol !== symbolRef.current) return;
      if (!isChartReady.current || !currentBarRef.current || !isSymbolLoadedRef.current) return;
      const price = parseFloat(data.close || data.c);
      if (isNaN(price) || price <= 0) return;
      const last = allDataRef.current[allDataRef.current.length - 1];
      if (last) {
        const updated = { ...last, high: Math.max(last.high, price), low: Math.min(last.low, price), close: price };
        allDataRef.current[allDataRef.current.length - 1] = updated;
        latestCandleRef.current = updated;
      }
    });
    return () => socket.disconnect();
  }, []);

  const handleTimeframeChange = (tf) => { setTimeframe(tf); timeframeRef.current = tf; };
  const handleReset = () => {
    const ts = chartRef.current?.timeScale();
    if (!ts) return;
    ts.fitContent();
    setTimeout(() => {
      ts.applyOptions({ rightOffset: 24, barSpacing: 12 });
    }, 50);
  };
  const handleZoomIn  = () => { const ts = chartRef.current?.timeScale(); if (ts) ts.applyOptions({ barSpacing: Math.min(ts.options().barSpacing * 1.15, 50) }); };
  const handleZoomOut = () => { const ts = chartRef.current?.timeScale(); if (ts) ts.applyOptions({ barSpacing: Math.max(ts.options().barSpacing * 0.85, 3) }); };
  const handleScroll  = (dir) => { const ts = chartRef.current?.timeScale(); if (ts) ts.scrollToPosition(ts.scrollPosition() + (dir === 'left' ? 10 : -10), true); };
  const statusColor   = connectionStatus === 'Connected' ? '#089981' : '#F23645';

  const wrapperStyle = isExpanded
    ? { position: 'fixed', inset: 0, width: '100vw', height: '100vh', zIndex: 9999, backgroundColor: '#161A25', overflow: 'hidden' }
    : { position: 'relative', width: '100%', backgroundColor: '#161A25', borderRadius: 8, overflow: 'hidden' };

  return (
    <div style={wrapperStyle}
         onMouseEnter={() => setIsHoveringControls(true)} onMouseLeave={() => setIsHoveringControls(false)}>

      <div style={{ position: 'absolute', top: 15, left: 15, right: 15, zIndex: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, background: 'rgba(42,46,57,0.85)', padding: '4px 10px', borderRadius: 4, borderLeft: `3px solid ${statusColor}` }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: '#FFF' }}>{connectionStatus}</span>
          <span style={{ fontSize: 11, fontWeight: 700, color: '#f0b429', marginLeft: 6 }}>
            {formatSymbolDisplay(symbol)} ({timeframe.toUpperCase()})
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <button
            onClick={() => setShowTDI(prev => !prev)}
            title="Show / Hide TDI Sharkfin Indicator"
            style={{
              padding: '6px 12px',
              background: showTDI ? 'rgba(0, 230, 118, 0.16)' : '#2A2E39',
              color: showTDI ? '#00E676' : '#787B86',
              border: `1px solid ${showTDI ? 'rgba(0, 230, 118, 0.45)' : 'rgba(255,255,255,0.12)'}`,
              cursor: 'pointer',
              borderRadius: 4,
              fontWeight: 700,
              fontSize: 11,
              fontFamily: 'monospace',
            }}>
            {showTDI ? '🦈 TDI: ON' : '🦈 TDI: OFF'}
          </button>

          <div style={{ display: 'flex', gap: 2, background: '#2A2E39', padding: 2, borderRadius: 4 }}>
            {['15m', '1h', '4h'].map(tf => (
              <button key={tf} onClick={() => handleTimeframeChange(tf)}
                style={{ padding: '6px 14px', background: timeframe === tf ? '#4B4B69' : 'transparent', color: timeframe === tf ? '#FFF' : '#787B86', border: 'none', cursor: 'pointer', borderRadius: 3 }}>
                {tf.toUpperCase()}
              </button>
            ))}
          </div>

          <button
            onClick={() => setIsExpanded(prev => !prev)}
            title={isExpanded ? 'Exit Fullscreen (ESC)' : 'Expand Chart Fullscreen'}
            style={{
              padding: '6px 12px',
              background: isExpanded ? '#ef5350' : '#2A2E39',
              color: '#FFF',
              border: '1px solid rgba(255,255,255,0.12)',
              cursor: 'pointer',
              borderRadius: 4,
              fontWeight: 700,
              fontSize: 11,
              display: 'flex',
              alignItems: 'center',
              gap: 5
            }}>
            {isExpanded ? '✕ EXIT FULLSCREEN' : '⛶ EXPAND'}
          </button>
        </div>
      </div>

      <div style={{ position: 'absolute', bottom: showTDI ? '26%' : 20, left: '50%', transform: 'translateX(-50%)', zIndex: 30, display: 'flex', gap: 8, opacity: isHoveringControls ? 1 : 0, transition: 'opacity 0.2s', background: 'rgba(42,46,57,0.9)', padding: '8px 12px', borderRadius: 30, border: '1px solid #374357' }}>
        <ControlButton onClick={() => handleScroll('left')}  label="<"     />
        <ControlButton onClick={handleZoomOut}               label="−"     />
        <ControlButton onClick={handleReset}                 label="RESET" wide />
        <ControlButton onClick={handleZoomIn}                label="+"     />
        <ControlButton onClick={() => handleScroll('right')} label=">"     />
      </div>

      <div ref={chartContainerRef} style={{ width: '100%', height: isExpanded ? '100vh' : 540, position: 'relative' }} />
    </div>
  );
};

const ControlButton = ({ onClick, label, wide }) => (
  <button onClick={onClick}
    style={{ background: '#363A45', color: '#FFF', border: 'none', borderRadius: 20, width: wide ? 60 : 30, height: 30, cursor: 'pointer', fontWeight: 'bold', fontSize: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'background 0.2s' }}
    onMouseOver={e => e.currentTarget.style.background = '#474D5C'}
    onMouseOut={e  => e.currentTarget.style.background = '#363A45'}>
    {label}
  </button>
);

export default ChartComponent;