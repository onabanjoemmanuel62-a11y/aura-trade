import React, { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, ColorType, CandlestickSeries, LineSeries } from 'lightweight-charts';
import axios from 'axios';
import io from 'socket.io-client';

// ☁️ LIVE CLOUD SERVER ADDRESS
const API_URL = 'https://aura-trade-v1.onrender.com';

// ==========================================
// 🎨 CUSTOM BOX PLUGIN (THE ENGINE)
// ==========================================
class BoxRenderer {
    constructor(data) { this._data = data; }
    draw(target) {
        target.useBitmapCoordinateSpace((scope) => {
            if (this._data.length === 0) return;
            const ctx = scope.context;
            const horizontalPixelRatio = scope.horizontalPixelRatio;
            const verticalPixelRatio = scope.verticalPixelRatio;

            this._data.forEach((zone) => {
                const x1 = zone.x !== undefined ? zone.x * horizontalPixelRatio : 0;
                const x2 = scope.mediaSize.width * horizontalPixelRatio;
                const yTop = zone.yTop * verticalPixelRatio;
                const yBottom = zone.yBottom * verticalPixelRatio;
                const width = x2 - x1;
                const height = yBottom - yTop; 

                ctx.fillStyle = zone.color;
                ctx.fillRect(x1, yTop, width, height);
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
        this._paneViews = [new BoxPaneView(this)];
    }
    setData(zones, series, timeScale) {
        this._rendererData = zones.map(zone => {
            const timeCoord = zone.time ? timeScale.timeToCoordinate(zone.time) : undefined;
            const priceTopCoord = series.priceToCoordinate(zone.top);
            const priceBottomCoord = series.priceToCoordinate(zone.bottom);
            if (priceTopCoord === null || priceBottomCoord === null) return null;
            return {
                x: timeCoord,
                yTop: Math.min(priceTopCoord, priceBottomCoord), 
                yBottom: Math.max(priceTopCoord, priceBottomCoord),
                color: zone.color
            };
        }).filter(z => z !== null);
    }
    updateAllViews() {}
    paneViews() { return this._paneViews; }
}

// ==========================================
// 🚀 MAIN REACT COMPONENT
// ==========================================
const ChartComponent = ({ levels, visuals, tradeSetup }) => {
  const chartContainerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null); 
  const fractalSeriesRef = useRef(null); 
  const boxPrimitiveRef = useRef(new BoxPrimitive());
  
  // Refs
  const isChartReady = useRef(false); // 🔒 SAFETY LOCK
  const currentBarRef = useRef(null);
  const socketRef = useRef(null);
  const timeframeRef = useRef('1h'); 
  const allDataRef = useRef([]);
  const isLoadingRef = useRef(false); 
  const latestCandleRef = useRef(null); 

  const [timeframe, setTimeframe] = useState('1h');
  const [prediction, setPrediction] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [connectionStatus, setConnectionStatus] = useState('Connecting...');
  const [newsData, setNewsData] = useState([]);
  const [isHoveringControls, setIsHoveringControls] = useState(false);

  // --- HELPER: Grid Snapping ---
  const getCandleStartTime = useCallback((timestamp, tf) => {
    let seconds = Number(timestamp);
    if (seconds > 2000000000) seconds = Math.floor(seconds / 1000);
    let resolution = tf === '4h' ? 14400 : 3600;
    return Math.floor(seconds / resolution) * resolution;
  }, []);

  // --- 1. INITIALIZE CHART ---
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: { background: { type: ColorType.Solid, color: '#161A25' }, textColor: '#D9D9D9' },
      grid: { vertLines: { color: '#2B2B43', style: 1 }, horzLines: { color: '#2B2B43', style: 1 } },
      width: chartContainerRef.current.clientWidth,
      height: 500,
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 20, barSpacing: 12 },
      rightPriceScale: { scaleMargins: { top: 0.2, bottom: 0.2 }, borderVisible: false, autoScale: true },
    });

    // ✅ V5 SYNTAX SAFEGUARD
    // Using addSeries(CandlestickSeries) which is correct for v5
    const newSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#089981', downColor: '#F23645', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#F23645'
    });
    
    const ghostSeries = chart.addSeries(LineSeries, {
        color: '#a855f7', lineWidth: 2, lineStyle: 2, title: 'AI PROJECTION'
    });

    newSeries.attachPrimitive(boxPrimitiveRef.current);

    chartRef.current = chart;
    candleSeriesRef.current = newSeries; 
    fractalSeriesRef.current = ghostSeries; 
    isChartReady.current = true; // 🔓 UNLOCK

    const handleResize = () => {
        if (chartContainerRef.current) chart.applyOptions({ width: chartContainerRef.current.clientWidth });
    };
    window.addEventListener('resize', handleResize);

    // 🕵️ HISTORY LISTENER
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (range && range.from < 5 && !isLoadingRef.current) {
            fetchOlderHistory();
        }
    });

    return () => { 
        isChartReady.current = false; // 🔒 LOCK
        window.removeEventListener('resize', handleResize); 
        chart.remove(); 
        chartRef.current = null;
        candleSeriesRef.current = null;
    };
  }, []);

  // --- 2. FETCH HISTORY ---
  const fetchOlderHistory = async () => {
      if (isLoadingRef.current || !allDataRef.current.length) return;
      isLoadingRef.current = true;
      const oldestTime = allDataRef.current[0].time; 
      
      try {
          const res = await axios.get(`${API_URL}/api/candles/${timeframeRef.current}`, {
              params: { limit: 500, before: oldestTime, timestamp: Date.now() }
          });
          const rawData = Array.isArray(res.data) ? res.data : [];
          if (rawData.length === 0) return;

          const newOldData = rawData.map(item => ({
            time: getCandleStartTime(item.time, timeframeRef.current), 
            open: parseFloat(item.open), high: parseFloat(item.high), low: parseFloat(item.low), close: parseFloat(item.close)
          })).sort((a, b) => a.time - b.time);

          // MERGE
          const combinedData = [...newOldData, ...allDataRef.current]
              .filter((v, i, a) => a.findIndex(t => (t.time === v.time)) === i);

          allDataRef.current = combinedData;
          if (candleSeriesRef.current) candleSeriesRef.current.setData(combinedData);

      } catch (err) { console.error("History Error", err); } 
      finally { isLoadingRef.current = false; }
  };

  // --- 3. LOAD INITIAL DATA ---
  useEffect(() => {
    if (!isChartReady.current) return;

    const loadInitialHistory = async () => {
      setIsLoading(true);
      try {
        const res = await axios.get(`${API_URL}/api/candles/${timeframe}`, {
            params: { limit: 1000, timestamp: Date.now() }
        });
        const data = (res.data || []).map(item => ({
            time: getCandleStartTime(item.time, timeframe), 
            open: parseFloat(item.open), high: parseFloat(item.high), low: parseFloat(item.low), close: parseFloat(item.close)
        })).sort((a, b) => a.time - b.time).filter((v,i,a)=>a.findIndex(t=>(t.time===v.time))===i);

        if (isChartReady.current && candleSeriesRef.current) {
            allDataRef.current = data;
            candleSeriesRef.current.setData(data);
            if (data.length > 0) currentBarRef.current = data[data.length - 1];
        }
      } catch (err) { console.error("Init Load Error", err); } 
      finally { setIsLoading(false); }
    };
    loadInitialHistory();
  }, [timeframe, getCandleStartTime]);

  // --- 4. ANIMATION LOOP ---
  useEffect(() => {
    let animationFrameId;
    const renderLoop = () => {
      if (!isChartReady.current) return; 

      if (candleSeriesRef.current && latestCandleRef.current) {
        candleSeriesRef.current.update(latestCandleRef.current);
        currentBarRef.current = latestCandleRef.current;
        latestCandleRef.current = null;
      }

      if (visuals?.smc_zones && chartRef.current && candleSeriesRef.current) {
          const rawZones = visuals.smc_zones.map(z => ({
              time: z.start_time || (Date.now() / 1000), 
              top: z.top, bottom: z.bottom,
              color: z.type === 'OB_BEAR' ? 'rgba(239, 83, 80, 0.25)' : 'rgba(38, 166, 154, 0.25)' 
          }));
          boxPrimitiveRef.current.setData(rawZones, candleSeriesRef.current, chartRef.current.timeScale());
      }
      animationFrameId = requestAnimationFrame(renderLoop);
    };
    renderLoop();
    return () => cancelAnimationFrame(animationFrameId);
  }, [visuals]);

  // --- 5. MARKERS & LINES (CRASH FIX HERE) ---
  useEffect(() => {
      const fetchNews = async () => {
          try {
              const res = await axios.get(`${API_URL}/api/news`, { params: { limit: 100 } });
              if (Array.isArray(res.data)) setNewsData(res.data.map(n => ({...n, time: n.time})));
          } catch (err) { console.error(err); }
      };
      fetchNews();
  }, []);

  useEffect(() => {
    if (newsData.length > 0 && isChartReady.current && candleSeriesRef.current) {
        try {
            const markers = newsData.filter(n => n.time).map(n => ({
                time: getCandleStartTime(n.time, timeframeRef.current),
                position: 'aboveBar',
                color: n.impact === 'High' ? '#ef5350' : '#ffa726',
                shape: 'arrowDown',
                text: n.impact === 'High' ? `🚩 ${n.event}` : '', 
                size: n.impact === 'High' ? 2 : 1,
            })).sort((a, b) => a.time - b.time); 
            
            // ✅ SAFETY CHECK: Prevent white screen if setMarkers isn't ready
            if (typeof candleSeriesRef.current.setMarkers === 'function') {
                candleSeriesRef.current.setMarkers(markers);
            }
        } catch (e) { console.warn("Marker skip", e); }
    }
  }, [newsData, timeframe, getCandleStartTime]);

  useEffect(() => {
    if (!candleSeriesRef.current || !isChartReady.current) return;
    const addLine = (price, color, title, isDashed = false, width = 2) => {
        if (price && !isNaN(parseFloat(price))) {
            candleSeriesRef.current.createPriceLine({
                price: parseFloat(price), color, lineWidth: width,
                lineStyle: isDashed ? 2 : 0, axisLabelVisible: true, title
            });
        }
    };
    if (tradeSetup) {
        if (tradeSetup.take_profit) addLine(tradeSetup.take_profit, '#00E676', 'TP 🎯', 0, 2); 
        if (tradeSetup.stop_loss) addLine(tradeSetup.stop_loss, '#FF1744', 'SL 🛑', 0, 2);     
        if (tradeSetup.entry) addLine(tradeSetup.entry, '#2962FF', 'ENTRY 🔵', 3, 2);          
    }
    if (levels && levels.ema) addLine(levels.ema, '#FFD700', '200 EMA', false, 1);
  }, [levels, tradeSetup]); 

  // --- 6. SOCKETS ---
  useEffect(() => {
    const socket = io(API_URL, { transports: ['polling'], path: '/socket.io/' });
    socketRef.current = socket;
    socket.on('connect', () => setConnectionStatus('Connected'));
    socket.on('disconnect', () => setConnectionStatus('Disconnected'));
    socket.on('price-update', (data) => {
        if (!isChartReady.current || !currentBarRef.current) return; 
        const price = parseFloat(data.close || data.c);
        if (isNaN(price)) return;
        
        const time = getCandleStartTime(data.time || Date.now(), timeframeRef.current);
        const lastCandle = allDataRef.current[allDataRef.current.length - 1];
        
        let updatedCandle;
        if (lastCandle && time === lastCandle.time) {
            updatedCandle = { ...lastCandle, high: Math.max(lastCandle.high, price), low: Math.min(lastCandle.low, price), close: price };
            allDataRef.current[allDataRef.current.length - 1] = updatedCandle;
        } else {
            updatedCandle = { time, open: price, high: price, low: price, close: price };
            allDataRef.current.push(updatedCandle);
        }
        latestCandleRef.current = updatedCandle;
    });
    socket.on('prediction-update', (data) => setPrediction(data));
    return () => socket.disconnect();
  }, [timeframe, getCandleStartTime]);

  // --- RENDER ---
  const handleTimeframeChange = (newTf) => { setTimeframe(newTf); timeframeRef.current = newTf; if(fractalSeriesRef.current) fractalSeriesRef.current.setData([]); };
  const handleReset = () => { if(chartRef.current) chartRef.current.timeScale().scrollToPosition(0, false); };
  const handleZoomIn = () => { if(chartRef.current) chartRef.current.timeScale().applyOptions({ barSpacing: chartRef.current.timeScale().options().barSpacing * 1.2 }); };
  const handleZoomOut = () => { if(chartRef.current) chartRef.current.timeScale().applyOptions({ barSpacing: chartRef.current.timeScale().options().barSpacing * 0.8 }); };
  const handleScroll = (dir) => { if(chartRef.current) { const pos = chartRef.current.timeScale().scrollPosition(); chartRef.current.timeScale().scrollToPosition(pos + (dir === 'left' ? 10 : -10), true); } };
  const getStatusColor = () => connectionStatus === 'Connected' ? '#089981' : '#F23645';

  return (
    <div style={{ position: 'relative', width: '100%', backgroundColor: '#161A25', borderRadius: '8px', overflow: 'hidden' }} onMouseEnter={() => setIsHoveringControls(true)} onMouseLeave={() => setIsHoveringControls(false)}>
      {isLoading && <div style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.5)', zIndex: 50, display: 'flex', justifyContent: 'center', alignItems: 'center' }}><div style={{ width: '40px', height: '40px', border: '4px solid #333', borderTop: '4px solid #089981', borderRadius: '50%', animation: 'spin 1s linear infinite' }} /></div>}
      
      <div style={{ position: 'absolute', top: '15px', left: '15px', right: '15px', zIndex: 20, display: 'flex', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(42, 46, 57, 0.8)', padding: '4px 10px', borderRadius: '4px', borderLeft: `3px solid ${getStatusColor()}` }}>
                <span style={{ fontSize: '11px', fontWeight: '600', color: '#FFF' }}>{connectionStatus}</span>
            </div>
            {prediction && <div style={{ background: 'rgba(30, 41, 59, 0.95)', padding: '10px 14px', borderRadius: '6px', border: '1px solid #334155' }}>
                <div style={{ fontSize: '10px', color: '#94A3B8' }}>AI Signal</div>
                <div style={{ fontSize: '18px', fontWeight: 'bold', color: parseFloat(prediction.probUp) > 55 ? '#089981' : '#F23645' }}>{parseFloat(prediction.probUp) > 55 ? 'BUY' : 'SELL'} <span style={{fontSize: '14px', color: '#FFF'}}>{Math.max(parseFloat(prediction.probUp||0), parseFloat(prediction.probDown||0)).toFixed(0)}%</span></div>
            </div>}
        </div>
        <div style={{ display: 'flex', gap: '2px', background: '#2A2E39', padding: '2px', borderRadius: '4px' }}>
            {['1h', '4h'].map(tf => <button key={tf} onClick={() => handleTimeframeChange(tf)} style={{ padding: '6px 14px', background: timeframe === tf ? '#4B4B69' : 'transparent', color: timeframe === tf ? '#FFF' : '#787B86', border: 'none', cursor: 'pointer' }}>{tf.toUpperCase()}</button>)}
        </div>
      </div>

      <div style={{ position: 'absolute', bottom: '20px', left: '50%', transform: 'translateX(-50%)', zIndex: 30, display: 'flex', gap: '8px', opacity: isHoveringControls ? 1 : 0, transition: 'opacity 0.2s', background: 'rgba(42, 46, 57, 0.9)', padding: '8px 12px', borderRadius: '30px', border: '1px solid #374357' }}>
          <ControlButton onClick={() => handleScroll('left')} label="<" />
          <ControlButton onClick={handleZoomOut} label="-" />
          <ControlButton onClick={handleReset} label="RESET" wide />
          <ControlButton onClick={handleZoomIn} label="+" />
          <ControlButton onClick={() => handleScroll('right')} label=">" />
      </div>

      <div ref={chartContainerRef} style={{ width: '100%', height: '500px' }} />
    </div>
  );
};

const ControlButton = ({ onClick, label, wide }) => (
    <button onClick={onClick} style={{ background: '#363A45', color: '#FFF', border: 'none', borderRadius: '20px', width: wide ? '60px' : '30px', height: '30px', cursor: 'pointer', fontWeight: 'bold', fontSize: '12px', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'background 0.2s' }} onMouseOver={(e) => e.currentTarget.style.background = '#474D5C'} onMouseOut={(e) => e.currentTarget.style.background = '#363A45'}>{label}</button>
);

export default ChartComponent;