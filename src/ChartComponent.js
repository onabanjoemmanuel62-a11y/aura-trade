import React, { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, ColorType, CandlestickSeries } from 'lightweight-charts';
import axios from 'axios';
import io from 'socket.io-client';

const API_URL = 'https://aura-trade-v1.onrender.com';

// ─────────────────────────────────────────────────────────────────────────────
// 🟥 PLUGIN 1: ORDER BLOCK BOXES
// - Active OBs extend to the RIGHT EDGE of visible chart (not full chart width)
// - Mitigated OBs end at their mitigation candle
// - Draws "1H-OB" and "ONLY BUYS / ONLY SELLS" labels inside the box
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
        // Active OBs → right edge; Mitigated OBs → their end candle
        const x2 = (zone.isMitigated && zone.x2 !== null && !isNaN(zone.x2))
          ? Math.min(zone.x2 * hPR, rightEdge)
          : rightEdge;

        if (x1 >= rightEdge) return;
        const width = x2 - x1;
        if (width <= 0) return;

        const yTop    = Math.min(zone.yTop, zone.yBottom) * vPR;
        const yBottom = Math.max(zone.yTop, zone.yBottom) * vPR;
        const height  = yBottom - yTop;
        if (height <= 0) return;

        // Fill
        ctx.fillStyle   = zone.fillColor;
        ctx.fillRect(x1, yTop, width, height);

        // Border
        ctx.strokeStyle  = zone.borderColor;
        ctx.lineWidth    = 2 * hPR;
        ctx.globalAlpha  = zone.isMitigated ? 0.4 : 1.0;
        ctx.setLineDash(zone.isMitigated ? [5 * hPR, 5 * hPR] : []);
        ctx.strokeRect(x1, yTop, width, height);
        ctx.setLineDash([]);
        ctx.globalAlpha = 1.0;

        // Labels — only on active boxes with enough height
        if (!zone.isMitigated && zone.label) {
          const fontSize   = Math.max(10, Math.min(13, height / vPR * 0.30)) * hPR;
          const pad        = 6 * hPR;
          ctx.font         = `bold ${fontSize}px monospace`;
          ctx.fillStyle    = zone.borderColor;
          ctx.fillText(zone.label, x1 + pad, yTop + fontSize * 1.2);
          if (zone.entryLabel && height > fontSize * 2.8) {
            ctx.font      = `${fontSize * 0.85}px monospace`;
            ctx.fillText(zone.entryLabel, x1 + pad, yTop + fontSize * 2.6);
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
        if (zone.mitigated_time) x2 = timeScale.timeToCoordinate(zone.mitigated_time);
      } catch { return null; }
      const yTop    = series.priceToCoordinate(zone.top);
      const yBottom = series.priceToCoordinate(zone.bottom);
      if (x1 === null || yTop === null || yBottom === null) return null;
      const isMitigated = zone.is_mitigated || false;
      const isBear      = zone.type === 'OB_BEAR';
      return {
        x:          x1,
        x2:         x2,
        yTop,
        yBottom,
        isMitigated,
        fillColor:   isMitigated
          ? (isBear ? 'rgba(239,83,80,0.06)' : 'rgba(38,166,154,0.06)')
          : (isBear ? 'rgba(239,83,80,0.18)' : 'rgba(38,166,154,0.18)'),
        borderColor: isBear ? 'rgba(239,83,80,1)' : 'rgba(38,166,154,1)',
        label:       zone.label       || (isBear ? '1H-OB (Bearish)' : '1H-OB (Bullish)'),
        entryLabel:  zone.entry_label || (isBear ? 'ONLY SELLS'       : 'ONLY BUYS'),
      };
    }).filter(Boolean);
  }

  paneViews() { return this._paneViews; }
}


// ─────────────────────────────────────────────────────────────────────────────
// 🔵 PLUGIN 2: BOS / CHoCH / ANCHOR LINES
// - Draws dashed line from start→BOS candle
// - Faded continuation to right edge
// - Anchor line is solid gold, CHoCH is orange, BOS is blue
// ─────────────────────────────────────────────────────────────────────────────
class BOSRenderer {
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

        // Main segment: start → BOS candle
        ctx.beginPath();
        ctx.moveTo(x1, y);
        ctx.lineTo(x2, y);
        ctx.strokeStyle = line.color;
        ctx.lineWidth   = line.isAnchor ? 1.5 * hPR : 2 * hPR;
        ctx.setLineDash(line.isAnchor ? [] : [7 * hPR, 5 * hPR]);
        ctx.stroke();

        // Faded continuation: BOS candle → right edge
        if (!line.isAnchor && x2 < rightEdge) {
          ctx.beginPath();
          ctx.moveTo(x2, y);
          ctx.lineTo(rightEdge, y);
          ctx.strokeStyle = line.color.replace(/[\d.]+\)$/, '0.25)');
          ctx.lineWidth   = 1 * hPR;
          ctx.setLineDash([3 * hPR, 8 * hPR]);
          ctx.stroke();
        }
        ctx.setLineDash([]);

        // Label
        const labelX = Math.min(x2 + 8 * hPR, rightEdge - 60 * hPR);
        ctx.font      = `bold ${12 * hPR}px monospace`;
        ctx.fillStyle = line.color;
        ctx.fillText(line.label, labelX, y - 4 * vPR);
      });
    });
  }
}

class BOSPaneView {
  constructor(source) { this._source = source; }
  renderer() { return new BOSRenderer(this._source._rendererData); }
}

class BOSPrimitive {
  constructor() {
    this._rendererData = [];
    this._paneViews    = [new BOSPaneView(this)];
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
        const isAnchor = line.type?.includes('Anchor') || line.type?.includes('PF');
        const isChoch  = line.is_choch || line.type?.includes('CHoCH');
        const color    = isAnchor ? 'rgba(255,215,0,0.9)'
                       : isChoch  ? 'rgba(255,165,0,0.9)'
                       : 'rgba(33,150,243,0.9)';
        return { x1, x2, y, color, label: line.type || 'BOS', isAnchor };
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
  const boxPrimitiveRef    = useRef(null);
  const bosPrimitiveRef    = useRef(null);
  const activeLinesRef     = useRef([]);
  const isChartReady       = useRef(false);
  const currentBarRef      = useRef(null);
  const timeframeRef       = useRef('1h');
  const allDataRef         = useRef([]);
  const isLoadingRef       = useRef(false);
  const latestCandleRef    = useRef(null);
  const symbolRef          = useRef(symbol);

  const [timeframe,          setTimeframe]          = useState('1h');
  const [connectionStatus,   setConnectionStatus]   = useState('Connecting...');
  const [newsData,           setNewsData]           = useState([]);
  const [isHoveringControls, setIsHoveringControls] = useState(false);

  // Wipe chart on symbol change
  useEffect(() => {
    symbolRef.current = symbol;
    if (candleSeriesRef.current) {
      allDataRef.current = [];
      candleSeriesRef.current.setData([]);
    }
  }, [symbol]);

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
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#089981', downColor: '#F23645', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#F23645',
    });
    const boxP = new BoxPrimitive();
    series.attachPrimitive(boxP);
    boxPrimitiveRef.current = boxP;
    const bosP = new BOSPrimitive();
    series.attachPrimitive(bosP);
    bosPrimitiveRef.current = bosP;
    chartRef.current = chart;
    candleSeriesRef.current = series;
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
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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
      if (candleSeriesRef.current) candleSeriesRef.current.setData(combined);
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
          candleSeriesRef.current.setData(data);
          if (data.length) currentBarRef.current = data[data.length - 1];
          setTimeout(() => chartRef.current?.timeScale().fitContent(), 50);
        }
      } catch { /* silent */ }
    };
    load();
  }, [timeframe, symbol, processCandles]);

  // Render loop
  useEffect(() => {
    let rafId;
    const loop = () => {
      if (!isChartReady.current) return;
      if (candleSeriesRef.current && latestCandleRef.current) {
        candleSeriesRef.current.update(latestCandleRef.current);
        currentBarRef.current  = latestCandleRef.current;
        latestCandleRef.current = null;
      }
      if (chartRef.current && candleSeriesRef.current) {
        const ts = chartRef.current.timeScale();
        if (visuals?.smc_zones && boxPrimitiveRef.current)
          boxPrimitiveRef.current.setData(visuals.smc_zones, candleSeriesRef.current, ts);
        if (visuals?.bos_lines && bosPrimitiveRef.current)
          bosPrimitiveRef.current.setData(visuals.bos_lines, candleSeriesRef.current, ts);
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

  // Price lines
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
    // ✅ Backend sends `ema200` — fall back to `ema` for safety
    const emaPrice = levels?.ema200 ?? levels?.ema;
    addLine(emaPrice, '#FFD700', '200 EMA', 2, 1);
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
  const handleReset   = () => chartRef.current?.timeScale().scrollToPosition(0, false);
  const handleZoomIn  = () => { const ts = chartRef.current?.timeScale(); if (ts) ts.applyOptions({ barSpacing: ts.options().barSpacing * 1.2 }); };
  const handleZoomOut = () => { const ts = chartRef.current?.timeScale(); if (ts) ts.applyOptions({ barSpacing: ts.options().barSpacing * 0.8 }); };
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
          {['1h', '4h'].map(tf => (
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

      <div ref={chartContainerRef} style={{ width: '100%', height: 500 }} />
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
