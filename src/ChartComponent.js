import React, { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, ColorType, CandlestickSeries, LineSeries } from 'lightweight-charts';
import axios from 'axios';
import io from 'socket.io-client';

const API_URL = 'https://aura-trade-v1.onrender.com';

// ─────────────────────────────────────────────────────────────────────────────
// 🧮 HELPER: Calculate Curved EMAs on the Frontend
// ─────────────────────────────────────────────────────────────────────────────
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
// 🟥 PLUGIN 1: MMM LEVEL CONSOLIDATION BOXES
// ─────────────────────────────────────────────────────────────────────────────
class BoxRenderer {
  constructor(data) { this._data = data; }

  draw(target) {
    target.useBitmapCoordinateSpace((scope) => {
      if (!this._data || this._data.length === 0) return;
      const ctx  = scope.context;
      const hPR  = scope.horizontalPixelRatio;
      const vPR  = scope.verticalPixelRatio;
      const rightEdge = scope.mediaSize.width * hPR;

      this._data.forEach((zone) => {
        if (zone.x === null || isNaN(zone.x)) return;

        const x1 = zone.x * hPR;
        const x2 = (zone.x2 !== null && !isNaN(zone.x2)) ? Math.min(zone.x2 * hPR, rightEdge) : rightEdge;

        if (x1 >= rightEdge) return;
        const width = x2 - x1;
        if (width <= 0) return;

        const yTop    = Math.min(zone.yTop, zone.yBottom) * vPR;
        const yBottom = Math.max(zone.yTop, zone.yBottom) * vPR;
        const height  = yBottom - yTop;
        if (height <= 0) return;

        // Fill Consolidation Box
        ctx.fillStyle = zone.fillColor;
        ctx.fillRect(x1, yTop, width, height);

        // Border
        ctx.strokeStyle = zone.borderColor;
        ctx.lineWidth   = 1.5 * hPR;
        ctx.setLineDash([]);
        ctx.strokeRect(x1, yTop, width, height);

        // Level Label
        if (zone.label) {
          const fontSize = Math.max(11, Math.min(14, height / vPR * 0.30)) * hPR;
          const pad      = 6 * hPR;
          ctx.font       = `bold ${fontSize}px monospace`;
          ctx.fillStyle  = zone.borderColor;
          ctx.fillText(zone.label, x1 + pad, yTop + fontSize * 1.2);
        }

        // Pullback entry zone — drawn to the right of the box
        if (zone.pullbackTop !== null && zone.pullbackBottom !== null) {
          const pyTop     = zone.pullbackTop * vPR;
          const pyBottom  = zone.pullbackBottom * vPR;
          const zoneX     = zone.x2 !== null ? zone.x2 * hPR : rightEdge;
          const zoneWidth = rightEdge - zoneX;
          if (zoneWidth > 0) {
            ctx.fillStyle = zone.isBear ? 'rgba(239,83,80,0.12)' : 'rgba(38,166,154,0.12)';
            ctx.fillRect(zoneX, pyTop, zoneWidth, pyBottom - pyTop);
            ctx.strokeStyle = zone.isBear ? 'rgba(239,83,80,0.6)' : 'rgba(38,166,154,0.6)';
            ctx.lineWidth = 1 * hPR;
            ctx.setLineDash([4 * hPR, 4 * hPR]);
            ctx.beginPath();
            ctx.moveTo(zoneX, pyTop);
            ctx.lineTo(rightEdge, pyTop);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(zoneX, pyBottom);
            ctx.lineTo(rightEdge, pyBottom);
            ctx.stroke();
            ctx.setLineDash([]);
            const lFontSize = 11 * hPR;
            ctx.font      = `bold ${lFontSize}px monospace`;
            ctx.fillStyle = zone.isBear ? 'rgba(239,83,80,0.9)' : 'rgba(38,166,154,0.9)';
            ctx.fillText(zone.pullbackLabel || 'ENTRY ZONE', zoneX + 6 * hPR, pyTop + lFontSize * 1.4);
          }
        }

      });
    });
  }
}

class BoxPaneView {
  constructor(source) { this._source = source; }
  renderer() { return new BoxRenderer(this._source._rendererData); }
}

class BoxPrimitive {
  constructor() {
    this._rendererData = [];
    this._paneViews    = [new BoxPaneView(this)];
  }

  setData(zones, series, timeScale) {
    if (!zones || !Array.isArray(zones)) { this._rendererData = []; return; }
    this._rendererData = zones.map((zone) => {
      if (!zone.time || zone.top == null || zone.bottom == null) return null;
      let x1 = null, x2 = null;
      try {
        x1 = timeScale.timeToCoordinate(zone.time);
        if (zone.end_time) x2 = timeScale.timeToCoordinate(zone.end_time);
      } catch { return null; }

      const yTop    = series.priceToCoordinate(zone.top);
      const yBottom = series.priceToCoordinate(zone.bottom);
      if (x1 === null || yTop === null || yBottom === null) return null;

      const isBear         = zone.type?.includes('BEAR');
      const pullbackTop    = zone.pullback_zone_top    ? series.priceToCoordinate(zone.pullback_zone_top)    : null;
      const pullbackBottom = zone.pullback_zone_bottom ? series.priceToCoordinate(zone.pullback_zone_bottom) : null;

      return {
        x:             x1,
        x2:            x2,
        yTop,
        yBottom,
        fillColor:     isBear ? 'rgba(239,83,80,0.08)' : 'rgba(38,166,154,0.08)',
        borderColor:   isBear ? 'rgba(239,83,80,0.8)'  : 'rgba(38,166,154,0.8)',
        label:         zone.label || 'CONSOLIDATION',
        isBear,
        pullbackTop,
        pullbackBottom,
        pullbackLabel: zone.pullback_label || 'ENTRY ZONE',
      };
    }).filter(Boolean);
  }

  paneViews() { return this._paneViews; }
}

// ─────────────────────────────────────────────────────────────────────────────
// 🔵 PLUGIN 2: PEAK ANCHORS & STOP HUNT PINS
// ─────────────────────────────────────────────────────────────────────────────
class LineRenderer {
  constructor(data) { this._data = data; }

  draw(target) {
    target.useBitmapCoordinateSpace((scope) => {
      if (!this._data || this._data.length === 0) return;
      const ctx  = scope.context;
      const hPR  = scope.horizontalPixelRatio;
      const vPR  = scope.verticalPixelRatio;
      const rightEdge = scope.mediaSize.width * hPR;

      this._data.forEach((line) => {
        if (line.x1 === null || line.x2 === null || line.y === null) return;
        const x1 = line.x1 * hPR;
        const x2 = Math.min(line.x2 * hPR, rightEdge);
        const y  = line.y  * vPR;
        if (x1 >= rightEdge) return;

        // Main line
        ctx.beginPath();
        ctx.moveTo(x1, y);
        ctx.lineTo(x2, y);
        ctx.strokeStyle = line.color;
        ctx.lineWidth   = line.isAnchor ? 2 * hPR : 1.5 * hPR;
        ctx.setLineDash(line.isAnchor ? [] : [6 * hPR, 4 * hPR]);
        ctx.stroke();
        ctx.setLineDash([]);

        // Label
        const labelX = Math.min(x2 + 8 * hPR, rightEdge - 80 * hPR);
        ctx.font      = `bold ${12 * hPR}px monospace`;
        ctx.fillStyle = line.color;
        ctx.fillText(line.label, labelX, y - 4 * vPR);
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
// 🚀 MAIN COMPONENT
// ─────────────────────────────────────────────────────────────────────────────
const ChartComponent = ({ symbol = 'GC=F', levels, visuals, tradeSetup }) => {
  const chartContainerRef  = useRef(null);
  const chartRef           = useRef(null);
  const candleSeriesRef    = useRef(null);
  const ema50SeriesRef     = useRef(null);
  const ema200SeriesRef    = useRef(null);
  
  const boxPrimitiveRef    = useRef(null);
  const linePrimitiveRef   = useRef(null);
  const activeLinesRef     = useRef([]);
  const isChartReady       = useRef(false);
  const currentBarRef      = useRef(null);
  const timeframeRef       = useRef('1h');
  const allDataRef         = useRef([]);
  const isLoadingRef       = useRef(false);
  const latestCandleRef    = useRef(null);
  const symbolRef          = useRef(symbol);

  const [timeframe,           setTimeframe]           = useState('1h');
  const [connectionStatus,   setConnectionStatus]   = useState('Connecting...');
  const [newsData,           setNewsData]           = useState([]);
  const [isHoveringControls, setIsHoveringControls] = useState(false);

  // 🔄 Update Series Data safely
  const updateChartData = useCallback((data) => {
    if (!candleSeriesRef.current || !data.length) return;
    candleSeriesRef.current.setData(data);
    if (ema50SeriesRef.current)  ema50SeriesRef.current.setData(calculateEMA(data, 50));
    if (ema200SeriesRef.current) ema200SeriesRef.current.setData(calculateEMA(data, 200));
  }, []);

  // Wipe chart on symbol change
  useEffect(() => {
    symbolRef.current = symbol;
    if (candleSeriesRef.current) {
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
      layout:  { background: { type: ColorType.Solid, color: '#161A25' }, textColor: '#D9D9D9' },
      grid:    { vertLines: { color: '#2B2B43', style: 1 }, horzLines: { color: '#2B2B43', style: 1 } },
      width:   chartContainerRef.current.clientWidth,
      height:  500,
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 20, barSpacing: 12 },
      rightPriceScale: { scaleMargins: { top: 0.2, bottom: 0.2 }, borderVisible: false, autoScale: true },
    });
    
    // ✅ NEW SYNTAX: chart.addSeries() is the only method supported in modern lightweight-charts
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#089981', downColor: '#F23645', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#F23645',
    });

    const ema50 = chart.addSeries(LineSeries, { color: '#2962FF', lineWidth: 2, crosshairMarkerVisible: false, priceLineVisible: false });
    const ema200 = chart.addSeries(LineSeries, { color: '#FFD700', lineWidth: 2, crosshairMarkerVisible: false, priceLineVisible: false });
    
   const drawSessionBoxes = (chart, series) => {
  const now = Math.floor(Date.now() / 1000);
  const dayStart = now - (now % 86400);
  const sessions = [
    { name: 'TOKYO',    start: 0,  end: 9,  color: 'rgba(124,58,237,0.08)',  border: 'rgba(124,58,237,0.4)' },
    { name: 'LONDON',   start: 8,  end: 17, color: 'rgba(14,165,233,0.08)',  border: 'rgba(14,165,233,0.4)'  },
    { name: 'NEW YORK', start: 13, end: 22, color: 'rgba(245,158,11,0.08)',  border: 'rgba(245,158,11,0.4)'  },
  ];

  // Draw for today and yesterday so chart always has visible boxes
  [-1, 0].forEach(dayOffset => {
    const base = dayStart + dayOffset * 86400;
    sessions.forEach(ses => {
      const startTime = base + ses.start * 3600;
      const endTime   = base + ses.end   * 3600;
      try {
        chart.addHistogramSeries && void 0; // just a guard
        const pane = chart.panes ? chart.panes()[0] : null;
        if (pane && pane.createPrimitive) {
          // lightweight-charts v5 native pane primitive
        }
      } catch { /* silent */ }

      // Use series markers as visible session labels on the time axis
      try {
        series.setMarkers([]);
      } catch { /* silent */ }
    });
  });

  // The real shading — inject colored divs over the chart canvas
  const container = chartContainerRef.current;
  if (!container) return;

  // Remove old session overlays
  container.querySelectorAll('.session-overlay').forEach(el => el.remove());

  sessions.forEach(ses => {
    [-1, 0].forEach(dayOffset => {
      const base = dayStart + dayOffset * 86400;
      const startTime = base + ses.start * 3600;
      const endTime   = base + ses.end   * 3600;

      try {
        const x1 = chart.timeScale().timeToCoordinate(startTime);
        const x2 = chart.timeScale().timeToCoordinate(endTime);
        if (x1 === null || x2 === null) return;
        if (x2 < 0 || x1 > container.clientWidth) return;

        const div = document.createElement('div');
        div.className = 'session-overlay';
        div.style.cssText = `
          position: absolute;
          top: 0;
          left: ${Math.max(0, x1)}px;
          width: ${Math.max(0, x2 - x1)}px;
          height: calc(100% - 30px);
          background: ${ses.color};
          border-left: 1px solid ${ses.border};
          border-right: 1px solid ${ses.border};
          pointer-events: none;
          z-index: 1;
        `;
        container.appendChild(div);
      } catch { /* silent */ }
    });
  });
};

    const boxP = new BoxPrimitive();
    series.attachPrimitive(boxP);
    boxPrimitiveRef.current = boxP;
    
    const lineP = new LinePrimitive();
    series.attachPrimitive(lineP);
    linePrimitiveRef.current = lineP;
    
    chartRef.current = chart;
    candleSeriesRef.current = series;
    ema50SeriesRef.current = ema50;
    ema200SeriesRef.current = ema200;
    isChartReady.current = true;
    
    const onResize = () => { if (chartContainerRef.current) chart.applyOptions({ width: chartContainerRef.current.clientWidth }); };
    window.addEventListener('resize', onResize);
    
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (range && range.from < 5 && !isLoadingRef.current) fetchOlderHistory();
    });
    
    return () => {
      isChartReady.current = false;
      window.removeEventListener('resize', onResize);
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      ema50SeriesRef.current = null;
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
        currentBarRef.current  = latestCandleRef.current;
        
        if (allDataRef.current.length) {
            if (ema50SeriesRef.current) ema50SeriesRef.current.setData(calculateEMA(allDataRef.current, 50));
            if (ema200SeriesRef.current) ema200SeriesRef.current.setData(calculateEMA(allDataRef.current, 200));
        }
        latestCandleRef.current = null;
      }
      
      if (chartRef.current && candleSeriesRef.current) {
        const ts = chartRef.current.timeScale();
        
        if (visuals?.smc_zones && boxPrimitiveRef.current) {
          boxPrimitiveRef.current.setData(visuals.smc_zones, candleSeriesRef.current, ts);
        }
        
        if (visuals?.bos_lines && linePrimitiveRef.current) {
           linePrimitiveRef.current.setData(visuals.bos_lines, candleSeriesRef.current, ts);
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

  // Price lines (Trade Setups only, horizontal EMAs removed)
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
      addLine(tradeSetup.take_profit, '#00E676', 'TP 🎯',    0, 2);
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
    ts.applyOptions({ rightOffset: 20, barSpacing: 12 });
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