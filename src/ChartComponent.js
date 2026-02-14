import React, { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, ColorType, CandlestickSeries } from 'lightweight-charts';
import axios from 'axios';
import io from 'socket.io-client';

// ☁️ LIVE CLOUD SERVER ADDRESS
const API_URL = 'https://aura-trade-v1.onrender.com';

const ChartComponent = ({ levels }) => {
  // --- REFS ---
  const chartContainerRef = useRef(null);
  const chartRef = useRef(null);
  
  // 1. SAFE REF NAME
  const candleSeriesRef = useRef(null); 
  
  // 2. ⚡ NEW: REF FOR TRENDLINES (To track and remove them)
  const linesRef = useRef({ resistance: null, support: null, ema: null });
  
  const currentBarRef = useRef(null);
  const socketRef = useRef(null);
  const timeframeRef = useRef('1h'); 
  
  // ⚡ DATA STORE
  const allDataRef = useRef([]);
  const isLoadingRef = useRef(false); 

  // ⚡ PERFORMANCE
  const latestCandleRef = useRef(null); 
  const isHistoryLoaded = useRef(false);

  // --- STATE ---
  const [timeframe, setTimeframe] = useState('1h');
  const [prediction, setPrediction] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [connectionStatus, setConnectionStatus] = useState('Connecting...');
  const [isHoveringControls, setIsHoveringControls] = useState(false);
  
  // 📰 News State
  const [newsData, setNewsData] = useState([]);

  // --- HELPER: Strict Grid Snapping ---
  const getCandleStartTime = useCallback((timestamp, tf) => {
    let seconds = Number(timestamp);
    if (seconds > 2000000000) seconds = Math.floor(seconds / 1000);
    
    let resolution = 3600; // 1h
    if (tf === '4h') resolution = 14400; // 4h
    
    return Math.floor(seconds / resolution) * resolution;
  }, []);

  // --- ANIMATION LOOP ---
  useEffect(() => {
    let animationFrameId;
    const renderLoop = () => {
      if (candleSeriesRef.current && latestCandleRef.current) {
        candleSeriesRef.current.update(latestCandleRef.current);
        currentBarRef.current = latestCandleRef.current;
        
        const lastIdx = allDataRef.current.length - 1;
        if (lastIdx >= 0) {
             const lastItem = allDataRef.current[lastIdx];
             if (lastItem.time === latestCandleRef.current.time) {
                 allDataRef.current[lastIdx] = latestCandleRef.current;
             } else {
                 allDataRef.current.push(latestCandleRef.current);
             }
        }
        latestCandleRef.current = null;
      }
      animationFrameId = requestAnimationFrame(renderLoop);
    };
    renderLoop();
    return () => cancelAnimationFrame(animationFrameId);
  }, []);

  // --- ⚡ NEW: DRAW TRENDLINES & LEVELS ---
  useEffect(() => {
    // Wait for chart series to be ready and data to exist
    if (!candleSeriesRef.current || !levels) return;

    // 1. Helper to clear old lines
    const clearLine = (key) => {
        if (linesRef.current[key]) {
            candleSeriesRef.current.removePriceLine(linesRef.current[key]);
            linesRef.current[key] = null;
        }
    };

    // Clear everything first
    clearLine('resistance');
    clearLine('support');
    clearLine('ema');

    // 2. Parse Data (Handle both Array and Object formats for safety)
    let resPrice, supPrice, emaPrice;

    if (Array.isArray(levels)) {
        resPrice = levels[0];
        supPrice = levels[1];
        emaPrice = levels[2];
    } else if (levels && typeof levels === 'object') {
        resPrice = levels.resistance;
        supPrice = levels.support;
        emaPrice = levels.ema;
    }

    // 3. Helper to create new lines
    const createLine = (price, color, title, style = 2, width = 2) => {
        if (price && !isNaN(parseFloat(price)) && parseFloat(price) > 0) {
            return candleSeriesRef.current.createPriceLine({
                price: parseFloat(price),
                color: color,
                lineWidth: width,
                lineStyle: style, // 2 = Dashed, 0 = Solid
                axisLabelVisible: true,
                title: title,
            });
        }
        return null;
    };

    // 4. Draw the lines
    // Resistance = RED (Dashed)
    linesRef.current.resistance = createLine(resPrice, '#ef5350', 'RESISTANCE', 2, 2);
    // Support = GREEN (Dashed)
    linesRef.current.support = createLine(supPrice, '#26a69a', 'SUPPORT', 2, 2);
    // EMA = YELLOW (Solid)
    linesRef.current.ema = createLine(emaPrice, '#FFD700', '200 EMA', 0, 1);

    console.log("📊 Chart Updated Lines:", { resPrice, supPrice, emaPrice });

  }, [levels]); // Re-run whenever 'levels' prop changes

  // --- 📰 STEP 1: FETCH NEWS DATA ---
  useEffect(() => {
      const fetchNews = async () => {
          try {
              console.log("📡 Fetching News from Cloud...");
              const res = await axios.get(`${API_URL}/api/news`, { params: { limit: 100 } });
              if (Array.isArray(res.data)) {
                  const formattedData = res.data.map(n => ({...n, time: n.time}));
                  setNewsData(formattedData);
              }
          } catch (err) {
              console.error("News Fetch Error:", err);
          }
      };
      fetchNews();
  }, []);

  // --- 📰 STEP 2: ROBUST MARKER RENDERING ---
  useEffect(() => {
    if (newsData.length > 0 && candleSeriesRef.current) {
        const markers = newsData
            .filter(n => n.time) 
            .map(n => {
                const time = getCandleStartTime(n.time, timeframeRef.current);
                const isHigh = n.impact === 'High';
                return {
                    time: time,
                    position: 'aboveBar',
                    color: isHigh ? '#ef5350' : '#ffa726',
                    shape: 'arrowDown',
                    text: isHigh ? `🚩 ${n.event}` : '', 
                    size: isHigh ? 2 : 1,
                };
            })
            .sort((a, b) => a.time - b.time); 

        try {
            if (candleSeriesRef.current && typeof candleSeriesRef.current.setMarkers === 'function') {
                candleSeriesRef.current.setMarkers(markers);
                console.log(`✅ Pinned ${markers.length} News Flags to the Chart`);
            } else {
                console.warn("⚠️ Chart Series not fully ready for markers yet.");
            }
        } catch (err) {
            console.error("❌ Failed to set markers:", err);
        }
    }
  }, [newsData, timeframe, getCandleStartTime]);

  // --- INTERNAL: FETCH OLDER HISTORY ---
  const fetchOlderHistory = async () => {
      if (isLoadingRef.current || !allDataRef.current.length) return;
      
      isLoadingRef.current = true;
      const oldestTime = allDataRef.current[0].time; 
      
      try {
          console.log("⚡ Fetching older history...");
          const res = await axios.get(`${API_URL}/api/candles/${timeframeRef.current}`, {
              params: { 
                  limit: 500, 
                  before: oldestTime, 
                  timestamp: Date.now() 
              }
          });

          const rawData = Array.isArray(res.data) ? res.data : [];
          if (rawData.length === 0) return;

          const newOldData = rawData.map(item => ({
            time: getCandleStartTime(item.time, timeframeRef.current), 
            open: parseFloat(item.open), 
            high: parseFloat(item.high), 
            low: parseFloat(item.low), 
            close: parseFloat(item.close)
          })).sort((a, b) => a.time - b.time);

          const combinedData = [...newOldData, ...allDataRef.current]
              .filter((v, i, a) => a.findIndex(t => (t.time === v.time)) === i);

          allDataRef.current = combinedData;
          if (candleSeriesRef.current) candleSeriesRef.current.setData(combinedData);

      } catch (err) {
          console.error("History Error:", err);
      } finally {
          isLoadingRef.current = false;
      }
  };

  // --- EFFECT 1: INITIALIZE CHART ---
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: { background: { type: ColorType.Solid, color: '#161A25' }, textColor: '#D9D9D9' },
      grid: { vertLines: { color: '#2B2B43', style: 1 }, horzLines: { color: '#2B2B43', style: 1 } },
      width: chartContainerRef.current.clientWidth,
      height: 500,
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 15, barSpacing: 12, minBarSpacing: 5 },
      rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.1 }, borderVisible: false, autoScale: true },
      crosshair: { mode: 1, vertLine: { labelVisible: true }, horzLine: { labelVisible: true, labelBackgroundColor: '#4CAF50' } }
    });

    const newSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#089981', downColor: '#F23645',
      borderVisible: false, wickUpColor: '#089981', wickDownColor: '#F23645',
      lastValueVisible: true, priceLineVisible: true, priceLineColor: '#4CAF50',
      priceLineWidth: 1, priceLineStyle: 2,
    });

    chartRef.current = chart;
    candleSeriesRef.current = newSeries; 

    const onVisibleLogicalRangeChanged = (newVisibleLogicalRange) => {
        if (newVisibleLogicalRange === null) return;
        if (newVisibleLogicalRange.from < 10) fetchOlderHistory();
    };

    chart.timeScale().subscribeVisibleLogicalRangeChange(onVisibleLogicalRangeChanged);

    const handleResize = () => {
        if (chartContainerRef.current) {
            chart.applyOptions({ width: chartContainerRef.current.clientWidth });
        }
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onVisibleLogicalRangeChanged);
      chart.remove();
    };
  }, []);

  // --- EFFECT 2: INITIAL LOAD ---
  useEffect(() => {
    if (!candleSeriesRef.current) return;

    const loadInitialHistory = async () => {
      setIsLoading(true);
      isLoadingRef.current = true;
      isHistoryLoaded.current = false;
      allDataRef.current = [];
      
      try {
        console.log("⚡ Fetching initial data...");
        const res = await axios.get(`${API_URL}/api/candles/${timeframe}`, {
            params: { limit: 500, timestamp: Date.now() }
        });
        
        const rawData = Array.isArray(res.data) ? res.data : [];
        const data = rawData.map(item => ({
            time: getCandleStartTime(item.time, timeframe), 
            open: parseFloat(item.open), 
            high: parseFloat(item.high), 
            low: parseFloat(item.low), 
            close: parseFloat(item.close)
        }))
        .filter((v, i, a) => a.findIndex(t => (t.time === v.time)) === i)
        .sort((a, b) => a.time - b.time);

        if (candleSeriesRef.current) {
            allDataRef.current = data;
            candleSeriesRef.current.setData(data);

            if (data.length > 0) {
                currentBarRef.current = data[data.length - 1];
            }
            
            chartRef.current.timeScale().scrollToPosition(0, false);
            chartRef.current.priceScale('right').applyOptions({ autoScale: true });
        }
        
        isHistoryLoaded.current = true; 

      } catch (err) {
        console.error("Load Error:", err);
      } finally {
        setIsLoading(false);
        isLoadingRef.current = false;
      }
    };

    loadInitialHistory();
  }, [timeframe, getCandleStartTime]); 

  // --- EFFECT 3: SOCKET ---
  useEffect(() => {
    const socket = io(API_URL, { transports: ['websocket'], reconnection: true });
    socketRef.current = socket;

    socket.on('connect', () => setConnectionStatus('Connected'));
    socket.on('disconnect', () => setConnectionStatus('Disconnected'));

    socket.on('price-update', (data) => {
        if (!isHistoryLoaded.current || !currentBarRef.current) return;

        const rawPrice = data.close || data.c || data.price || data.p || data.value;
        const price = parseFloat(rawPrice);
        if (isNaN(price)) return;

        const currentTf = timeframeRef.current;
        let rawTime = data.time || data.t || Date.now();
        let seconds = Number(rawTime);
        if (seconds > 2000000000) seconds = Math.floor(seconds / 1000);
        let resolution = currentTf === '4h' ? 14400 : 3600;
        const bucketTime = Math.floor(seconds / resolution) * resolution;

        const lastCandle = currentBarRef.current;
        let updatedCandle;

        if (bucketTime === lastCandle.time) {
            updatedCandle = { ...lastCandle, high: Math.max(lastCandle.high, price), low: Math.min(lastCandle.low, price), close: price };
        } else if (bucketTime > lastCandle.time) {
            updatedCandle = { time: bucketTime, open: price, high: price, low: price, close: price };
        } else {
            return;
        }

        latestCandleRef.current = updatedCandle;
        
        // 🧠 SMART AUTO-SCROLL
        if (chartRef.current) {
             const scrollPos = chartRef.current.timeScale().scrollPosition();
             const distanceToLive = Math.abs(scrollPos);

             if (distanceToLive < 5) {
                 chartRef.current.timeScale().scrollToPosition(0, false);
             }
        }
    });

    let lastPredUpdate = 0;
    socket.on('prediction-update', (data) => {
        const now = Date.now();
        if (now - lastPredUpdate > 500) {
            setPrediction(data);
            lastPredUpdate = now;
        }
    });

    return () => {
        socket.disconnect();
    };
  }, []); 

  // --- HANDLERS ---
  const handleTimeframeChange = (newTf) => {
      if (newTf === timeframe) return;
      setTimeframe(newTf);
      timeframeRef.current = newTf; 
      isHistoryLoaded.current = false;
  };

  // 🚀 FIXED: HARD SNAP RESET
  const handleReset = () => {
    if (chartRef.current) {
        // 1. Instant Jump (false = no animation)
        chartRef.current.timeScale().scrollToPosition(0, false);
        // 2. Reset Zoom
        chartRef.current.timeScale().applyOptions({ barSpacing: 12 });
        // 3. Fix Scaling
        chartRef.current.priceScale('right').applyOptions({ autoScale: true });
    }
  };
  
  const handleZoomIn = () => { if(chartRef.current) chartRef.current.timeScale().applyOptions({ barSpacing: chartRef.current.timeScale().options().barSpacing * 1.2 }); }
  const handleZoomOut = () => { if(chartRef.current) chartRef.current.timeScale().applyOptions({ barSpacing: chartRef.current.timeScale().options().barSpacing * 0.8 }); }
  const handleScroll = (dir) => {
      if(chartRef.current) {
          const pos = chartRef.current.timeScale().scrollPosition();
          chartRef.current.timeScale().scrollToPosition(pos + (dir === 'left' ? 10 : -10), true);
      }
  }

  const getStatusColor = () => connectionStatus === 'Connected' ? '#089981' : '#F23645';

  return (
    <div 
        style={{ position: 'relative', width: '100%', backgroundColor: '#161A25', borderRadius: '8px', overflow: 'hidden' }}
        onMouseEnter={() => setIsHoveringControls(true)} 
        onMouseLeave={() => setIsHoveringControls(false)}
    >
      
      {/* LOADING SPINNER */}
      {isLoading && (
        <div style={{
            position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
            backgroundColor: 'rgba(22, 26, 37, 0.4)', zIndex: 50,
            display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center',
            backdropFilter: 'blur(2px)', pointerEvents: 'none'
        }}>
            <div style={{ 
                width: '40px', height: '40px', border: '4px solid #333', 
                borderTop: '4px solid #089981', borderRadius: '50%', 
                animation: 'spin 1s linear infinite' 
            }} />
        </div>
      )}

      {/* HEADER */}
      <div style={{ position: 'absolute', top: '15px', left: '15px', right: '15px', zIndex: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(42, 46, 57, 0.8)', padding: '4px 10px', borderRadius: '4px', borderLeft: `3px solid ${getStatusColor()}` }}>
                <span style={{ fontSize: '11px', fontWeight: '600', color: '#FFF' }}>{connectionStatus}</span>
            </div>
            {prediction && (
                 <div style={{ background: 'rgba(30, 41, 59, 0.95)', padding: '10px 14px', borderRadius: '6px', border: '1px solid #334155', backdropFilter: 'blur(4px)' }}>
                    <div style={{ fontSize: '10px', color: '#94A3B8', textTransform: 'uppercase' }}>AI Signal</div>
                    <div style={{ fontSize: '18px', fontWeight: 'bold', color: parseFloat(prediction.probUp) > 55 ? '#089981' : '#F23645', marginTop: '2px' }}>
                         {parseFloat(prediction.probUp) > 55 ? 'BUY' : 'SELL'} 
                         <span style={{fontSize: '14px', marginLeft: '6px', color: '#FFF'}}>{Math.max(parseFloat(prediction.probUp||0), parseFloat(prediction.probDown||0)).toFixed(0)}%</span>
                    </div>
                 </div>
            )}
        </div>

        <div style={{ display: 'flex', gap: '2px', background: '#2A2E39', padding: '2px', borderRadius: '4px' }}>
            {['1h', '4h'].map(tf => (
                <button key={tf} onClick={() => handleTimeframeChange(tf)} style={{
                        padding: '6px 14px', backgroundColor: timeframe === tf ? '#4B4B69' : 'transparent',
                        color: timeframe === tf ? '#FFF' : '#787B86', border: 'none', borderRadius: '4px', cursor: 'pointer', fontWeight: '600', fontSize: '13px'
                }}>
                    {tf.toUpperCase()}
                </button>
            ))}
        </div>
      </div>

      {/* --- GHOST CONTROLS --- */}
      <div style={{
          position: 'absolute', bottom: '20px', left: '50%', transform: 'translateX(-50%)',
          zIndex: 30, display: 'flex', gap: '8px',
          opacity: isHoveringControls ? 1 : 0, 
          transition: 'opacity 0.2s ease-in-out',
          background: 'rgba(42, 46, 57, 0.9)', padding: '8px 12px', borderRadius: '30px',
          boxShadow: '0 4px 6px rgba(0,0,0,0.3)', border: '1px solid #374357'
      }}>
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
    <button 
        onClick={onClick}
        style={{
            background: '#363A45', color: '#FFF', border: 'none', borderRadius: '20px',
            width: wide ? '60px' : '30px', height: '30px', cursor: 'pointer',
            fontWeight: 'bold', fontSize: '12px', display: 'flex', alignItems: 'center', justifyContent: 'center',
            transition: 'background 0.2s'
        }}
        onMouseOver={(e) => e.currentTarget.style.background = '#474D5C'}
        onMouseOut={(e) => e.currentTarget.style.background = '#363A45'}
    >
        {label}
    </button>
);

export default ChartComponent;