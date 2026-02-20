import React, { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, ColorType, CandlestickSeries, LineSeries } from 'lightweight-charts';
import axios from 'axios';
import io from 'socket.io-client';

// ☁️ LIVE CLOUD SERVER ADDRESS
const API_URL = 'https://aura-trade-v1.onrender.com';

// ==========================================
// 🎨 CUSTOM BOX PLUGIN (TRUNCATES MITIGATED OBS)
// ==========================================
class BoxRenderer {
    constructor(data) { this._data = data; }
    draw(target) {
        target.useBitmapCoordinateSpace((scope) => {
            if (!this._data || this._data.length === 0) return;
            const ctx = scope.context;
            const horizontalPixelRatio = scope.horizontalPixelRatio;
            const verticalPixelRatio = scope.verticalPixelRatio;

            this._data.forEach((zone) => {
                if (zone.x === null || isNaN(zone.x)) return;

                const x1 = zone.x * horizontalPixelRatio;
                // 🛑 THE MAGIC: If it has an end time (x2), stop drawing there. 
                // Otherwise, stretch to the far right edge of the screen.
                const x2 = (zone.x2 !== null && !isNaN(zone.x2)) 
                    ? zone.x2 * horizontalPixelRatio 
                    : scope.mediaSize.width * horizontalPixelRatio; 
                
                const yTop = zone.yTop * verticalPixelRatio;
                const yBottom = zone.yBottom * verticalPixelRatio;
                const width = x2 - x1;
                const height = Math.abs(yBottom - yTop); 

                // Safety check: skip drawing if the box dimensions are negative/zero
                if (width <= 0) return; 

                ctx.fillStyle = zone.color;
                ctx.fillRect(x1, Math.min(yTop, yBottom), width, height);

                if (zone.borderColor) {
                    ctx.strokeStyle = zone.borderColor;
                    // 🎨 Make mitigated blocks faint with a dashed border
                    if (zone.isMitigated) {
                        ctx.setLineDash([4 * horizontalPixelRatio, 4 * horizontalPixelRatio]);
                        ctx.lineWidth = 1 * horizontalPixelRatio;
                    } else {
                        ctx.setLineDash([]);
                        ctx.lineWidth = 1.5 * horizontalPixelRatio;
                    }
                    ctx.strokeRect(x1, Math.min(yTop, yBottom), width, height);
                    ctx.setLineDash([]); // Reset dash for the next item
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
        this._paneViews = [new BoxPaneView(this)];
    }
    setData(zones, series, timeScale) {
        if (!zones || !Array.isArray(zones)) {
            this._rendererData = [];
            return;
        }

        this._rendererData = zones.map(zone => {
            if (!zone.time || !zone.top || !zone.bottom) return null;
            
            let timeCoord = null;
            let timeCoord2 = null; 
            
            try {
                 timeCoord = timeScale.timeToCoordinate(zone.time);
                 // If Python says it's mitigated, map the end coordinate
                 if (zone.mitigated_time) {
                     timeCoord2 = timeScale.timeToCoordinate(zone.mitigated_time);
                 }
            } catch(e) {
                 return null; // Time is off-screen, safe to skip rendering
            }

            const priceTopCoord = series.priceToCoordinate(zone.top);
            const priceBottomCoord = series.priceToCoordinate(zone.bottom);
            
            if (priceTopCoord === null || priceBottomCoord === null || timeCoord === null) return null;
            
            // Define styling based on mitigation status
            const isMitigated = zone.is_mitigated || false;
            let fillColor, borderColor;

            if (isMitigated) {
                fillColor = zone.type === 'OB_BEAR' ? 'rgba(239, 83, 80, 0.04)' : 'rgba(38, 166, 154, 0.04)';
                borderColor = zone.type === 'OB_BEAR' ? 'rgba(239, 83, 80, 0.3)' : 'rgba(38, 166, 154, 0.3)';
            } else {
                fillColor = zone.type === 'OB_BEAR' ? 'rgba(239, 83, 80, 0.15)' : 'rgba(38, 166, 154, 0.15)';
                borderColor = zone.type === 'OB_BEAR' ? 'rgba(239, 83, 80, 0.8)' : 'rgba(38, 166, 154, 0.8)';
            }

            return {
                x: timeCoord, 
                x2: timeCoord2, // Will be null for active blocks, stretching them to infinity
                yTop: Math.min(priceTopCoord, priceBottomCoord), 
                yBottom: Math.max(priceTopCoord, priceBottomCoord),
                color: fillColor,
                borderColor: borderColor,
                isMitigated: isMitigated
            };
        }).filter(z => z !== null);
    }
    updateAllViews() {
         if(this._paneViews[0] && this._paneViews[0].update) {
             this._paneViews[0].update();
         }
    }
    paneViews() { return this._paneViews; }
}

// ==========================================
// 🚀 MAIN REACT COMPONENT
// ==========================================
const ChartComponent = ({ levels, visuals, tradeSetup }) => {
  const chartContainerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null); 
  const boxPrimitiveRef = useRef(null); 
  const activeLinesRef = useRef([]);

  const isChartReady = useRef(false);
  const currentBarRef = useRef(null);
  const timeframeRef = useRef('1h'); 
  const allDataRef = useRef([]);
  const isLoadingRef = useRef(false); 
  const latestCandleRef = useRef(null); 

  const [timeframe, setTimeframe] = useState('1h');
  const [connectionStatus, setConnectionStatus] = useState('Connecting...');
  const [newsData, setNewsData] = useState([]);
  const [isHoveringControls, setIsHoveringControls] = useState(false);

  const getCandleStartTime = useCallback((timestamp, tf) => {
    let seconds = Number(timestamp);
    if (seconds > 2000000000) seconds = Math.floor(seconds / 1000);
    let resolution = tf === '4h' ? 14400 : 3600;
    return Math.floor(seconds / resolution) * resolution;
  }, []);

  const processCandles = useCallback((rawData, tf) => {
      if (!Array.isArray(rawData)) return [];
      return rawData
          .filter(d => d.open != null && d.close != null) 
          .map(d => ({
              time: getCandleStartTime(d.time, tf),
              open: parseFloat(d.open),
              high: parseFloat(d.high),
              low: parseFloat(d.low),
              close: parseFloat(d.close)
          }))
          .filter(d => !isNaN(d.open) && !isNaN(d.close) && d.open > 0) 
          .sort((a, b) => a.time - b.time)
          .filter((v, i, a) => a.findIndex(t => t.time === v.time) === i); 
  }, [getCandleStartTime]);

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

    const newSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#089981', downColor: '#F23645', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#F23645'
    });
    
    const primitive = new BoxPrimitive();
    newSeries.attachPrimitive(primitive);
    boxPrimitiveRef.current = primitive;

    chartRef.current = chart;
    candleSeriesRef.current = newSeries; 
    isChartReady.current = true;

    const handleResize = () => {
        if (chartContainerRef.current) chart.applyOptions({ width: chartContainerRef.current.clientWidth });
    };
    window.addEventListener('resize', handleResize);

    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (range && range.from < 5 && !isLoadingRef.current) {
            fetchOlderHistory();
        }
    });

    return () => { 
        isChartReady.current = false;
        window.removeEventListener('resize', handleResize); 
        chart.remove(); 
        chartRef.current = null;
        candleSeriesRef.current = null;
    };
  }, []);

  const fetchOlderHistory = async () => {
      if (isLoadingRef.current || !allDataRef.current.length) return;
      isLoadingRef.current = true;
      const oldestTime = allDataRef.current[0].time; 
      
      try {
          const res = await axios.get(`${API_URL}/api/candles/${timeframeRef.current}`, {
              params: { limit: 500, before: oldestTime, timestamp: Date.now() }
          });
          
          const newValidData = processCandles(res.data, timeframeRef.current);
          if (newValidData.length === 0) return;

          const combinedData = [...newValidData, ...allDataRef.current]
              .filter((v, i, a) => a.findIndex(t => (t.time === v.time)) === i);

          allDataRef.current = combinedData;
          if (candleSeriesRef.current) candleSeriesRef.current.setData(combinedData);

      } catch (err) { console.error("History Error", err); } 
      finally { isLoadingRef.current = false; }
  };

  useEffect(() => {
    if (!isChartReady.current) return;

    const loadInitialHistory = async () => {
      try {
        const res = await axios.get(`${API_URL}/api/candles/${timeframe}`, {
            params: { limit: 1000, timestamp: Date.now() }
        });
        
        const validData = processCandles(res.data, timeframe);

        if (isChartReady.current && candleSeriesRef.current) {
            allDataRef.current = validData;
            candleSeriesRef.current.setData(validData);
            if (validData.length > 0) currentBarRef.current = validData[validData.length - 1];
        }
      } catch (err) { console.error("Init Load Error", err); } 
    };
    loadInitialHistory();
  }, [timeframe, getCandleStartTime, processCandles]);

  // --- 🎨 RENDER VISUALS (ORDER BLOCKS) ---
  useEffect(() => {
    let animationFrameId;
    const renderLoop = () => {
      if (!isChartReady.current) return; 

      if (candleSeriesRef.current && latestCandleRef.current) {
        candleSeriesRef.current.update(latestCandleRef.current);
        currentBarRef.current = latestCandleRef.current;
        latestCandleRef.current = null;
      }

      // 2. Feed zones to the BoxPrimitive
      if (visuals?.smc_zones && chartRef.current && candleSeriesRef.current && boxPrimitiveRef.current) {
          boxPrimitiveRef.current.setData(visuals.smc_zones, candleSeriesRef.current, chartRef.current.timeScale());
      }

      animationFrameId = requestAnimationFrame(renderLoop);
    };
    renderLoop();
    return () => cancelAnimationFrame(animationFrameId);
  }, [visuals]);

  // --- 📰 RENDER NEWS MARKERS ---
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
            
            if (candleSeriesRef.current && typeof candleSeriesRef.current.setMarkers === 'function') {
                candleSeriesRef.current.setMarkers(markers);
            }
        } catch (e) { console.warn("Marker skip", e); }
    }
  }, [newsData, timeframe, getCandleStartTime]);

  // --- 🎯 RENDER LEVELS (EMA, TP, SL) ---
  useEffect(() => {
    if (!candleSeriesRef.current || !isChartReady.current) return;
    
    activeLinesRef.current.forEach(line => {
        try { candleSeriesRef.current.removePriceLine(line); } catch (e) {}
    });
    activeLinesRef.current = []; 

    const addLine = (price, color, title, isDashed = false, width = 2) => {
        if (price && !isNaN(parseFloat(price))) {
            const line = candleSeriesRef.current.createPriceLine({
                price: parseFloat(price), color, lineWidth: width,
                lineStyle: isDashed ? 2 : 0, axisLabelVisible: true, title
            });
            activeLinesRef.current.push(line); 
        }
    };

    if (tradeSetup) {
        if (tradeSetup.take_profit) addLine(tradeSetup.take_profit, '#00E676', 'TP 🎯', 0, 2); 
        if (tradeSetup.stop_loss) addLine(tradeSetup.stop_loss, '#FF1744', 'SL 🛑', 0, 2);     
        if (tradeSetup.entry) addLine(tradeSetup.entry, '#2962FF', 'ENTRY 🔵', 3, 2);          
    }
    if (levels && levels.ema) addLine(levels.ema, '#FFD700', '200 EMA', false, 1);

  }, [levels, tradeSetup]); 

  // --- ⚡ WEBSOCKET CONNECTION ---
  useEffect(() => {
    const socket = io(API_URL, { transports: ['polling'], path: '/socket.io/' });
    socket.on('connect', () => setConnectionStatus('Connected'));
    socket.on('disconnect', () => setConnectionStatus('Disconnected'));
    socket.on('price-update', (data) => {
        if (!isChartReady.current || !currentBarRef.current) return; 
        const price = parseFloat(data.close || data.c);
        
        if (isNaN(price) || price <= 0) return; 
        
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
    return () => socket.disconnect();
  }, [timeframe, getCandleStartTime]);

  const handleTimeframeChange = (newTf) => { setTimeframe(newTf); timeframeRef.current = newTf; };
  const handleReset = () => { if(chartRef.current) chartRef.current.timeScale().scrollToPosition(0, false); };
  const handleZoomIn = () => { if(chartRef.current) chartRef.current.timeScale().applyOptions({ barSpacing: chartRef.current.timeScale().options().barSpacing * 1.2 }); };
  const handleZoomOut = () => { if(chartRef.current) chartRef.current.timeScale().applyOptions({ barSpacing: chartRef.current.timeScale().options().barSpacing * 0.8 }); };
  const handleScroll = (dir) => { if(chartRef.current) { const pos = chartRef.current.timeScale().scrollPosition(); chartRef.current.timeScale().scrollToPosition(pos + (dir === 'left' ? 10 : -10), true); } };
  const getStatusColor = () => connectionStatus === 'Connected' ? '#089981' : '#F23645';

  return (
    <div style={{ position: 'relative', width: '100%', backgroundColor: '#161A25', borderRadius: '8px', overflow: 'hidden' }} onMouseEnter={() => setIsHoveringControls(true)} onMouseLeave={() => setIsHoveringControls(false)}>
      
      <div style={{ position: 'absolute', top: '15px', left: '15px', right: '15px', zIndex: 20, display: 'flex', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(42, 46, 57, 0.8)', padding: '4px 10px', borderRadius: '4px', borderLeft: `3px solid ${getStatusColor()}` }}>
                <span style={{ fontSize: '11px', fontWeight: '600', color: '#FFF' }}>{connectionStatus}</span>
            </div>
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