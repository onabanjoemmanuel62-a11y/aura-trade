import React, { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, ColorType, CandlestickSeries, LineSeries } from 'lightweight-charts';
import axios from 'axios';
import io from 'socket.io-client';

const API_URL = 'https://aura-trade-v1.onrender.com';

// ─────────────────────────────────────────────────────────────────────────────
// 🧮 HELPER: Precision & Curved EMAs on the Frontend
// ─────────────────────────────────────────────────────────────────────────────
const getPriceFormat = (sym = '') => {
  const s = sym.toUpperCase();
  if (s.includes('GC=F') || s.includes('XAU') || s.includes('GOLD')) {
    return { type: 'price', precision: 2, minMove: 0.01 };
  }
  if (s.includes('JPY')) {
    return { type: 'price', precision: 3, minMove: 0.001 };
  }
  return { type: 'price', precision: 5, minMove: 0.00001 };
};

const calculateEMA = (data, period) => {
  if (!data || data.length === 0) return [];
  const k = 2 / (period + 1);
  let ema = data[0].close;
  return data.map(d => {
    ema = (d.close - ema) * k + ema;
    return { time: d.time, value: ema };
  });
};

// ─────────────────────────────────────────────────────────────────────────────
// 🔵 PEAK FORMATION & TARGET LINES (Peak High/Low + Target 1 & Target 2)
// ─────────────────────────────────────────────────────────────────────────────
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
        const x1 = line.x1 * hPR;
        const x2 = Math.min(line.x2 * hPR, rightEdge);
        const y  = line.y  * vPR;
        if (x1 >= rightEdge) return;

        ctx.beginPath();
        ctx.moveTo(x1, y);
        ctx.lineTo(x2, y);
        ctx.strokeStyle = line.color;
        ctx.lineWidth   = line.isAnchor ? 2.2 * hPR : 1.6 * hPR;
        ctx.setLineDash(line.isAnchor ? [] : [6 * hPR, 4 * hPR]);
        ctx.stroke();
        ctx.setLineDash([]);

        const labelX  = Math.min(x2 + 8 * hPR, rightEdge - 145 * hPR);
        ctx.font      = `bold ${11 * hPR}px monospace`;
        ctx.fillStyle = line.color;
        ctx.fillText(line.label, labelX, y - 5 * vPR);
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

  setData(lines, series, timeScale) {
    if (!lines || !Array.isArray(lines)) { this._rendererData = []; return; }
    this._rendererData = lines.map((line) => {
      if (!line.start_time || !line.end_time || line.level == null) return null;
      try {
        const x1 = timeScale.timeToCoordinate(line.start_time);
        const x2 = timeScale.timeToCoordinate(line.end_time);
        const y  = series.priceToCoordinate(line.level);
        if (x1 === null || x2 === null || y === null) return null;

        const isAnchor = line.type?.includes('Anchor') || line.type?.includes('Peak');
        const color    = line.color || (isAnchor ? 'rgba(255,215,0,0.9)' : 'rgba(239,83,80,0.8)');

        return { x1, x2, y, color, label: line.type || 'LEVEL', isAnchor };
      } catch { return null; }
    }).filter(Boolean);
  }

  paneViews() { return this._paneViews; }
}

// ─────────────────────────────────────────────────────────────────────────────
// 🟩 DEMAND/SUPPLY TRENDLINE (sloped — separate from the horizontal peak line)
// ─────────────────────────────────────────────────────────────────────────────
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

      const px1 = x1 * hPR, py1 = y1 * vPR;
      const px2 = Math.min(x2, rightEdge / hPR) * hPR, py2 = y2 * vPR;
      if (px1 >= rightEdge) return;

      ctx.beginPath();
      ctx.moveTo(px1, py1);
      ctx.lineTo(px2, py2);
      ctx.strokeStyle = color;
      ctx.lineWidth   = 2 * hPR;
      ctx.stroke();

      ctx.font      = `bold ${11 * hPR}px monospace`;
      ctx.fillStyle = color;
      ctx.fillText(label, Math.min(px2 + 6 * hPR, rightEdge - 115 * hPR), py2 - 6 * vPR);
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

  setData(tl, series, timeScale) {
    if (!tl || !tl.start_time || !tl.end_time || tl.start_level == null || tl.end_level == null) {
      this._rendererData = null;
      return;
    }
    try {
      const x1 = timeScale.timeToCoordinate(tl.start_time);
      const x2 = timeScale.timeToCoordinate(tl.end_time);
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

// ─────────────────────────────────────────────────────────────────────────────
// 🟨 ORDER BLOCK ZONE (uses dynamic label from backend)
// ─────────────────────────────────────────────────────────────────────────────
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
        if (zone.x1 === null || zone.yTop === null) return;
        const x1      = zone.x1 * hPR;
        const x2      = rightEdge;
        const yTop    = zone.yTop * vPR;
        const yBottom = zone.yBottom * vPR;
        if (x1 >= rightEdge) return;

        ctx.fillStyle   = zone.isBullish ? 'rgba(38,166,154,0.11)' : 'rgba(239,83,80,0.11)';
        ctx.fillRect(x1, yTop, x2 - x1, yBottom - yTop);
        ctx.strokeStyle = zone.isBullish ? 'rgba(38,166,154,0.75)' : 'rgba(239,83,80,0.75)';
        ctx.lineWidth   = 1 * hPR;
        ctx.strokeRect(x1, yTop, x2 - x1, yBottom - yTop);

        ctx.font      = `bold ${10 * hPR}px monospace`;
        ctx.fillStyle = zone.isBullish ? 'rgba(38,166,154,0.95)' : 'rgba(239,83,80,0.95)';
        ctx.fillText(zone.label || 'ORDER BLOCK', x1 + 6 * hPR, yTop + 13 * vPR);
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

  setData(obs, series, timeScale) {
    if (!obs || !Array.isArray(obs)) { this._rendererData = []; return; }
    this._rendererData = obs.map((ob) => {
      if (!ob.time || ob.top == null || ob.bottom == null) return null;
      try {
        const x1      = timeScale.timeToCoordinate(ob.time);
        const yTop    = series.priceToCoordinate(ob.top);
        const yBottom = series.priceToCoordinate(ob.bottom);
        if (x1 === null || yTop === null || yBottom === null) return null;
        return {
          x1,
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

// ─────────────────────────────────────────────────────────────────────────────
// 🏹 2-LEG PROJECTED ROADMAP ARROW (Now -> Pullback Zone -> Target 2)
// ─────────────────────────────────────────────────────────────────────────────
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

      const x0 = p0.x * hPR;
      const y0 = p0.y * vPR;
      // Project 2 legs neatly into the right-margin whitespace (like reference chart)
      const x1 = Math.min(x0 + 28 * hPR, rightEdge - 65 * hPR);
      const y1 = p1.y * vPR;
      const x2 = Math.min(x0 + 78 * hPR, rightEdge - 20 * hPR);
      const y2 = p2.y * vPR;

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

      // Draw Arrowhead at (x2, y2)
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

  setData(path, series, timeScale) {
    if (!path || !Array.isArray(path) || path.length < 3) {
      this._rendererData = null;
      return;
    }
    try {
      const x0 = timeScale.timeToCoordinate(path[0].time);
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

// ─────────────────────────────────────────────────────────────────────────────
// 🚀 MAIN COMPONENT
// ─────────────────────────────────────────────────────────────────────────────
const ChartComponent = ({ symbol = 'GC=F', levels, visuals, tradeSetup }) => {
  const chartContainerRef       = useRef(null);
  const chartRef                = useRef(null);
  const candleSeriesRef         = useRef(null);
  const ema50SeriesRef          = useRef(null);
  const ema200SeriesRef         = useRef(null);

  const linePrimitiveRef        = useRef(null);
  const trendlinePrimitiveRef   = useRef(null);
  const obZonePrimitiveRef      = useRef(null);
  const projectionPrimitiveRef  = useRef(null);
  const activeLinesRef          = useRef([]);
  const isChartReady            = useRef(false);
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

  const updateChartData = useCallback((data) => {
    if (!candleSeriesRef.current || !data.length) return;
    candleSeriesRef.current.setData(data);
    if (ema50SeriesRef.current)  ema50SeriesRef.current.setData(calculateEMA(data, 50));
    if (ema200SeriesRef.current) ema200SeriesRef.current.setData(calculateEMA(data, 200));
  }, []);

  // Update precision & wipe chart on symbol change
  useEffect(() => {
    symbolRef.current = symbol;
    if (candleSeriesRef.current) {
      const pf = getPriceFormat(symbol);
      candleSeriesRef.current.applyOptions({ priceFormat: pf });
      if (ema50SeriesRef.current)  ema50SeriesRef.current.applyOptions({ priceFormat: pf });
      if (ema200SeriesRef.current) ema200SeriesRef.current.applyOptions({ priceFormat: pf });
      allDataRef.current = [];
      updateChartData([]);
    }
  }, [symbol, updateChartData]);

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

  // Chart init (once)
  useEffect(() => {
    if (!chartContainerRef.current) return;
    const chart = createChart(chartContainerRef.current, {
      layout:          { background: { type: ColorType.Solid, color: '#161A25' }, textColor: '#D9D9D9' },
      grid:            { vertLines: { color: '#2B2B43', style: 1 }, horzLines: { color: '#2B2B43', style: 1 } },
      width:           chartContainerRef.current.clientWidth,
      height:          500,
      timeScale:       { timeVisible: true, secondsVisible: false, rightOffset: 24, barSpacing: 12 },
      rightPriceScale: { scaleMargins: { top: 0.18, bottom: 0.18 }, borderVisible: false, autoScale: true },
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

    const ema50  = chart.addSeries(LineSeries, { color: '#2962FF', lineWidth: 2, crosshairMarkerVisible: false, priceLineVisible: false, priceFormat: initialFormat });
    const ema200 = chart.addSeries(LineSeries, { color: '#FFD700', lineWidth: 2, crosshairMarkerVisible: false, priceLineVisible: false, priceFormat: initialFormat });

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

    chartRef.current        = chart;
    candleSeriesRef.current = series;
    ema50SeriesRef.current  = ema50;
    ema200SeriesRef.current = ema200;
    isChartReady.current    = true;

    const onResize = () => {
      if (chartContainerRef.current) chart.applyOptions({ width: chartContainerRef.current.clientWidth });
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
      ema50SeriesRef.current  = null;
      ema200SeriesRef.current = null;
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

  // Load on timeframe / symbol change
  useEffect(() => {
    if (!isChartReady.current) return;
    const load = async () => {
      try {
        const res = await axios.get(`${API_URL}/api/candles/${timeframe}`, {
          params: { symbol, limit: 1000, timestamp: Date.now() },
        });
        const data = processCandles(res.data);
        if (isChartReady.current && candleSeriesRef.current) {
          allDataRef.current = data;
          updateChartData(data);
          if (data.length) currentBarRef.current = data[data.length - 1];
          setTimeout(() => chartRef.current?.timeScale().fitContent(), 50);
        }
      } catch { /* silent */ }
    };
    load();
  }, [timeframe, symbol, processCandles, updateChartData]);

  // Render loop for Primitives & Live EMAs
  useEffect(() => {
    let rafId;
    const loop = () => {
      if (!isChartReady.current) return;

      if (candleSeriesRef.current && latestCandleRef.current) {
        candleSeriesRef.current.update(latestCandleRef.current);
        currentBarRef.current = latestCandleRef.current;

        if (allDataRef.current.length) {
          if (ema50SeriesRef.current)  ema50SeriesRef.current.setData(calculateEMA(allDataRef.current, 50));
          if (ema200SeriesRef.current) ema200SeriesRef.current.setData(calculateEMA(allDataRef.current, 200));
        }
        latestCandleRef.current = null;
      }

      if (chartRef.current && candleSeriesRef.current) {
        const ts = chartRef.current.timeScale();

        if (linePrimitiveRef.current) {
          linePrimitiveRef.current.setData(visuals?.bos_lines || [], candleSeriesRef.current, ts);
        }
        if (trendlinePrimitiveRef.current) {
          trendlinePrimitiveRef.current.setData(visuals?.trendline, candleSeriesRef.current, ts);
        }
        if (obZonePrimitiveRef.current) {
          obZonePrimitiveRef.current.setData(visuals?.order_blocks || [], candleSeriesRef.current, ts);
        }
        if (projectionPrimitiveRef.current) {
          projectionPrimitiveRef.current.setData(visuals?.projection_path, candleSeriesRef.current, ts);
        }
      }
      rafId = requestAnimationFrame(loop);
    };
    loop();
    return () => cancelAnimationFrame(rafId);
  }, [visuals]);

  // News markers
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

  // Price lines (Active Trade Setups)
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
      if (tradeSetup.target_2) {
        addLine(tradeSetup.target_2, '#00E676', 'TP2 🚀', 2, 2);
      }
      addLine(tradeSetup.take_profit, '#00E676', 'TP1 🎯',   0, 2);
      addLine(tradeSetup.stop_loss,   '#FF1744', 'SL 🛑',    0, 2);
      addLine(tradeSetup.entry,       '#2962FF', 'ENTRY 🔵', 3, 2);
    }
  }, [levels, tradeSetup]);

  // Socket
  useEffect(() => {
    const socket = io(API_URL, { transports: ['polling'], path: '/socket.io/' });
    socket.on('connect',    () => setConnectionStatus('Connected'));
    socket.on('disconnect', () => setConnectionStatus('Disconnected'));
    socket.on('price-update', (data) => {
      if (data.symbol !== symbolRef.current) return;
      if (!isChartReady.current || !currentBarRef.current) return;
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

  return (
    <div style={{ position: 'relative', width: '100%', backgroundColor: '#161A25', borderRadius: 8, overflow: 'hidden' }}
         onMouseEnter={() => setIsHoveringControls(true)} onMouseLeave={() => setIsHoveringControls(false)}>

      {/* Status + timeframe switcher */}
      <div style={{ position: 'absolute', top: 15, left: 15, right: 15, zIndex: 20, display: 'flex', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, background: 'rgba(42,46,57,0.85)', padding: '4px 10px', borderRadius: 4, borderLeft: `3px solid ${statusColor}` }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: '#FFF' }}>{connectionStatus}</span>
        </div>
        <div style={{ display: 'flex', gap: 2, background: '#2A2E39', padding: 2, borderRadius: 4 }}>
          {['15m', '1h', '4h'].map(tf => (
            <button key={tf} onClick={() => handleTimeframeChange(tf)}
              style={{ padding: '6px 14px', background: timeframe === tf ? '#4B4B69' : 'transparent', color: timeframe === tf ? '#FFF' : '#787B86', border: 'none', cursor: 'pointer', borderRadius: 3 }}>
              {tf.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {/* Zoom / scroll controls */}
      <div style={{ position: 'absolute', bottom: 20, left: '50%', transform: 'translateX(-50%)', zIndex: 30, display: 'flex', gap: 8, opacity: isHoveringControls ? 1 : 0, transition: 'opacity 0.2s', background: 'rgba(42,46,57,0.9)', padding: '8px 12px', borderRadius: 30, border: '1px solid #374357' }}>
        <ControlButton onClick={() => handleScroll('left')}  label="<"     />
        <ControlButton onClick={handleZoomOut}               label="−"     />
        <ControlButton onClick={handleReset}                 label="RESET" wide />
        <ControlButton onClick={handleZoomIn}                label="+"     />
        <ControlButton onClick={() => handleScroll('right')} label=">"     />
      </div>

      <div ref={chartContainerRef} style={{ width: '100%', height: 500, position: 'relative' }} />
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