import React, { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, ColorType, CandlestickSeries, LineSeries } from 'lightweight-charts'; // 1. Added LineSeries
import axios from 'axios';
import io from 'socket.io-client';

// ☁️ LIVE CLOUD SERVER ADDRESS
const API_URL = 'https://aura-trade.onrender.com';

const ChartComponent = () => {
  // --- REFS ---
  const chartContainerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null); 
  const ghostSeriesRef = useRef(null); 
  const trendlineSeriesRef = useRef(null); 
  
  const currentBarRef = useRef(null);
  const socketRef = useRef(null);
  const timeframeRef = useRef('1h'); 
  
  const allDataRef = useRef([]);
  const isLoadingRef = useRef(false); 
  const latestCandleRef = useRef(null); 
  const isHistoryLoaded = useRef(false);

  // --- STATE ---
  const [timeframe, setTimeframe] = useState('1h');
  const [prediction, setPrediction] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [connectionStatus, setConnectionStatus] = useState('Connecting...');
  const [isHoveringControls, setIsHoveringControls] = useState(false);
  const [newsData, setNewsData] = useState([]);

  // --- HELPER: Strict Grid Snapping ---
  const getCandleStartTime = useCallback((timestamp, tf) => {
    let seconds = Number(timestamp);
    if (seconds > 2000000000) seconds = Math.floor(seconds / 1000);
    let resolution = 3600; 
    if (tf === '4h') resolution = 14400; 
    return Math.floor(seconds / resolution) * resolution;
  }, []);

  // --- 🔮 GHOST PATTERN RENDERER (v4 Compatible) ---
  const renderGhostPattern = useCallback((ghostPath) => {
    // 🛡️ SAFETY: Stop if chart or data is missing
    if (!ghostPath || ghostPath.length === 0 || !currentBarRef.current || !chartRef.current) return;

    try {
        // Initialize the ghost series if it doesn't exist
        if (!ghostSeriesRef.current) {
            // 2. FIXED: Use addSeries(LineSeries, options) instead of addLineSeries
            ghostSeriesRef.current = chartRef.current.addSeries(LineSeries, {
                color: '#A855F7', // 🔮 AI Purple
                lineWidth: 2,
                lineStyle: 2,     // Dashed Line
                crosshairMarkerVisible: false,
                lastValueVisible: false,
                priceLineVisible: false,
            });
        }

        // Scale & Shift Logic
        const currentPrice = currentBarRef.current.close;
        const startOfGhost = ghostPath[0];
        const multiplier = currentPrice / startOfGhost;
        const lastTime = currentBarRef.current.time;
        let timeStep = timeframeRef.current === '4h' ? 14400 : 3600;

        const projectedData = ghostPath.map((price, index) => ({
            time: lastTime + ((index + 1) * timeStep),
            value: price * multiplier
        }));

        ghostSeriesRef.current.setData(projectedData);
    } catch (err) {
        console.error("👻 Ghost Render Error:", err.message);
    }
  }, []);

  // --- 📉 TRENDLINE RENDERER (v4 Compatible) ---
  const renderTrendlines = useCallback((keyLevels) => {
      if (!keyLevels || !chartRef.current) return;

      if (!trendlineSeriesRef.current) {
          // 2. FIXED: v4 Syntax here too
          trendlineSeriesRef.current = chartRef.current.addSeries(LineSeries, {
              color: '#3b82f6', 
              lineWidth: 1,
              lineStyle: 3, 
              lastValueVisible: true,
          });
      }
      // Logic to draw lines would go here
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

  // --- INITIAL CHART SETUP ---
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: { background: { type: ColorType.Solid, color: '#161A25' }, textColor: '#D9D9D9' },
      grid: { vertLines: { color: '#2B2B43', style: 1 }, horzLines: { color: '#2B2B43', style: 1 } },
      width: chartContainerRef.current.clientWidth,
      height: 500,
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 50, barSpacing: 12 },
      rightPriceScale: { autoScale: true },
      crosshair: { mode: 1 }
    });

    const newSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#089981', downColor: '#F23645', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#F23645',
    });

    chartRef.current = chart;
    candleSeriesRef.current = newSeries; 

    const handleResize = () => {
        if (chartContainerRef.current) chart.applyOptions({ width: chartContainerRef.current.clientWidth });
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      chart.remove();
      chartRef.current = null; 
    };
  }, []);

  // --- LOAD HISTORY ---
  useEffect(() => {
    if (!candleSeriesRef.current) return;

    const loadInitialHistory = async () => {
      setIsLoading(true);
      isLoadingRef.current = true;
      isHistoryLoaded.current = false;
      allDataRef.current = [];
      
      try {
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
            if (data.length > 0) currentBarRef.current = data[data.length - 1];
            
            chartRef.current.timeScale().scrollToPosition(0, false);
            chartRef.current.priceScale('right').applyOptions({ autoScale: true });
        }
        isHistoryLoaded.current = true; 
      } catch (err) { console.error("Load Error:", err); }
      finally { setIsLoading(false); isLoadingRef.current = false; }
    };
    loadInitialHistory();
  }, [timeframe, getCandleStartTime]); 

  // --- SOCKET CONNECTION ---
  useEffect(() => {
    const socket = io(API_URL, { transports: ['websocket'], reconnection: true });
    socketRef.current = socket;

    socket.on('connect', () => setConnectionStatus('Connected'));
    socket.on('disconnect', () => setConnectionStatus('Disconnected'));

    socket.on('price-update', (data) => {
        if (!isHistoryLoaded.current || !currentBarRef.current) return;
        const rawPrice = parseFloat(data.close || data.c);
        if (isNaN(rawPrice)) return;

        const currentTf = timeframeRef.current;
        let resolution = currentTf === '4h' ? 14400 : 3600;
        const bucketTime = Math.floor(Number(data.time) / resolution) * resolution;
        
        const lastCandle = currentBarRef.current;
        let updatedCandle;

        if (bucketTime === lastCandle.time) {
            updatedCandle = { ...lastCandle, high: Math.max(lastCandle.high, rawPrice), low: Math.min(lastCandle.low, rawPrice), close: rawPrice };
        } else if (bucketTime > lastCandle.time) {
            updatedCandle = { time: bucketTime, open: rawPrice, high: rawPrice, low: rawPrice, close: rawPrice };
        } else { return; }

        latestCandleRef.current = updatedCandle;
    });

    // 🧠 AI PREDICTION LISTENER
    socket.on('prediction-update', (data) => {
        // Anti-Flicker: Only update if signal changes
        setPrediction(prev => {
            if (prev && prev.signal === data.signal && prev.confidence === data.confidence) return prev;
            return data;
        });
        
        if (data.ghostPath && data.ghostPath.length > 0) {
            // Delay slightly to allow chart to be ready
            setTimeout(() => renderGhostPattern(data.ghostPath), 100);
        }
    });

    return () => { socket.disconnect(); };
  }, [renderGhostPattern]); 

  // --- HANDLERS ---
  const handleTimeframeChange = (newTf) => {
      if (newTf === timeframe) return;
      setTimeframe(newTf);
      timeframeRef.current = newTf; 
      isHistoryLoaded.current = false;
      
      // CLEANUP: Remove ghost series when switching timeframes
      if (ghostSeriesRef.current && chartRef.current) {
          chartRef.current.removeSeries(ghostSeriesRef.current);
          ghostSeriesRef.current = null;
      }
  };

  const handleReset = () => {
    if (chartRef.current) {
        chartRef.current.timeScale().scrollToPosition(0, false);
        chartRef.current.timeScale().applyOptions({ barSpacing: 12 });
        chartRef.current.priceScale('right').applyOptions({ autoScale: true });
    }
  };
  
  const handleZoomIn = () => { if(chartRef.current) chartRef.current.timeScale().applyOptions({ barSpacing: chartRef.current.timeScale().options().barSpacing * 1.2 }); }
  const handleZoomOut = () => { if(chartRef.current) chartRef.current.timeScale().applyOptions({ barSpacing: chartRef.current.timeScale().options().barSpacing * 0.8 }); }
  
  const getStatusColor = () => connectionStatus === 'Connected' ? '#089981' : '#F23645';

  return (
    <div 
        style={{ position: 'relative', width: '100%', backgroundColor: '#161A25', borderRadius: '8px', overflow: 'hidden' }}
        onMouseEnter={() => setIsHoveringControls(true)} 
        onMouseLeave={() => setIsHoveringControls(false)}
    >
      {/* LOADING SPINNER */}
      {isLoading && (
        <div style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(22, 26, 37, 0.4)', zIndex: 50, display: 'flex', justifyContent: 'center', alignItems: 'center', backdropFilter: 'blur(2px)' }}>
            <div style={{ width: '40px', height: '40px', border: '4px solid #333', borderTop: '4px solid #089981', borderRadius: '50%', animation: 'spin 1s linear infinite' }} />
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
                    <div style={{ fontSize: '18px', fontWeight: 'bold', color: prediction.signal === 'BUY' ? '#089981' : prediction.signal === 'SELL' ? '#F23645' : '#FFF', marginTop: '2px' }}>
                          {prediction.signal} 
                          <span style={{fontSize: '14px', marginLeft: '6px', color: '#FFF'}}>{prediction.confidence}%</span>
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
      <div style={{ position: 'absolute', bottom: '20px', left: '50%', transform: 'translateX(-50%)', zIndex: 30, display: 'flex', gap: '8px', opacity: isHoveringControls ? 1 : 0, transition: 'opacity 0.2s', background: 'rgba(42, 46, 57, 0.9)', padding: '8px 12px', borderRadius: '30px', boxShadow: '0 4px 6px rgba(0,0,0,0.3)', border: '1px solid #374357' }}>
          <ControlButton onClick={handleZoomOut} label="-" />
          <ControlButton onClick={handleReset} label="RESET" wide />
          <ControlButton onClick={handleZoomIn} label="+" />
      </div>

      <div ref={chartContainerRef} style={{ width: '100%', height: '500px' }} />
      <style>{`@keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }`}</style>
    </div>
  );
};

const ControlButton = ({ onClick, label, wide }) => (
    <button onClick={onClick} style={{ background: '#363A45', color: '#FFF', border: 'none', borderRadius: '20px', width: wide ? '60px' : '30px', height: '30px', cursor: 'pointer', fontWeight: 'bold', fontSize: '12px', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'background 0.2s' }}>
        {label}
    </button>
);

export default ChartComponent;