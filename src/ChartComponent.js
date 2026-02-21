import React, { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, ColorType, CandlestickSeries, LineSeries } from 'lightweight-charts';
import axios from 'axios';
import io from 'socket.io-client';

// ☁️ LIVE CLOUD SERVER ADDRESS
const API_URL = 'https://aura-trade-v1.onrender.com';

// ==========================================
// 🎨 1. CUSTOM BOX PLUGIN (HIGH VISIBILITY)
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
                const x2 = (zone.x2 !== null && !isNaN(zone.x2)) 
                    ? zone.x2 * horizontalPixelRatio 
                    : scope.mediaSize.width * horizontalPixelRatio; 
                
                const yTop = zone.yTop * verticalPixelRatio;
                const yBottom = zone.yBottom * verticalPixelRatio;
                
                // 🛑 GUARANTEED VISIBILITY
                let width = x2 - x1;
                if (zone.isMitigated) {
                    width = Math.max(width + (12 * horizontalPixelRatio), 20 * horizontalPixelRatio);
                }

                if (width <= 0 && zone.x2 !== null) return; 

                ctx.fillStyle = zone.color;
                const height = Math.abs(yBottom - yTop);
                ctx.fillRect(x1, Math.min(yTop, yBottom), width, height);

                if (zone.borderColor) {
                    ctx.strokeStyle = zone.borderColor;
                    if (zone.isMitigated) {
                        ctx.setLineDash([6 * horizontalPixelRatio, 6 * horizontalPixelRatio]);
                        ctx.lineWidth = 1.5 * horizontalPixelRatio; 
                    } else {
                        ctx.setLineDash([]);
                        ctx.lineWidth = 2.5 * horizontalPixelRatio; 
                    }
                    ctx.strokeRect(x1, Math.min(yTop, yBottom), width, height);
                    ctx.setLineDash([]); 
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
                 if (zone.mitigated_time) {
                     timeCoord2 = timeScale.timeToCoordinate(zone.mitigated_time);
                 }
            } catch(e) {
                 return null; 
            }

            const priceTopCoord = series.priceToCoordinate(zone.top);
            const priceBottomCoord = series.priceToCoordinate(zone.bottom);
            
            if (priceTopCoord === null || priceBottomCoord === null || timeCoord === null) return null;
            
            const isMitigated = zone.is_mitigated || false;
            let fillColor, borderColor;

            if (isMitigated) {
                fillColor = zone.type === 'OB_BEAR' ? 'rgba(239, 83, 80, 0.15)' : 'rgba(38, 166, 154, 0.15)';
                borderColor = zone.type === 'OB_BEAR' ? 'rgba(239, 83, 80, 0.9)' : 'rgba(38, 166, 154, 0.9)';
            } else {
                fillColor = zone.type === 'OB_BEAR' ? 'rgba(239, 83, 80, 0.3)' : 'rgba(38, 166, 154, 0.3)';
                borderColor = zone.type === 'OB_BEAR' ? 'rgba(239, 83, 80, 1)' : 'rgba(38, 166, 154, 1)';
            }

            return {
                x: timeCoord, 
                x2: timeCoord2, 
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
// 🎨 2. NEW PLUGIN: THICK BOS / CHoCH LINES
// ==========================================
class BOSRenderer {
    constructor(data) { this._data = data; }
    draw(target) {
        target.useBitmapCoordinateSpace((scope) => {
            if (!this._data || this._data.length === 0) return;
            const ctx = scope.context;
            const horizontalPixelRatio = scope.horizontalPixelRatio;
            const verticalPixelRatio = scope.verticalPixelRatio;

            this._data.forEach((line) => {
                if (line.x1 === null || line.x2 === null || line.y === null) return;
                
                const x1 = line.x1 * horizontalPixelRatio;
                const x2 = line.x2 * horizontalPixelRatio;
                const y = line.y * verticalPixelRatio;

                const brightColor = line.color.replace(/0\.\d+\)/, '1)');

                ctx.beginPath();
                ctx.moveTo(x1, y);
                ctx.lineTo(x2 + (15 * horizontalPixelRatio), y); 
                ctx.strokeStyle = brightColor;
                ctx.lineWidth = 2.5 * horizontalPixelRatio; 
                ctx.setLineDash([8 * horizontalPixelRatio, 6 * horizontalPixelRatio]); 
                ctx.stroke();
                ctx.setLineDash([]);

                const labelText = line.label || "BOS";
                ctx.font = `bold ${13 * horizontalPixelRatio}px sans-serif`; 
                ctx.fillStyle = brightColor;
                ctx.fillText(labelText, x2 + (20 * horizontalPixelRatio), y + (4 * horizontalPixelRatio));
            });
        });
    }
}

class BOSPaneView {
    constructor(source) { this._source = source; }
    renderer() { return new BOSRenderer(this._source._rendererData); }
}

class BOSPrimitive {
    constructor() { this._rendererData = []; this._paneViews = [new BOSPaneView(this)]; }
    setData(lines, series, timeScale) {
        if (!lines || !Array.isArray(lines)) { this._rendererData = []; return; }
        this._rendererData = lines.map(line => {
            if (!line.start_time || !line.end_time || !line.level) return null;
            try {
                const x1 = timeScale.timeToCoordinate(line.start_time);
                const x2 = timeScale.timeToCoordinate(line.end_time);
                const y = series.priceToCoordinate(line.level);
                if (x1 === null || x2 === null || y === null) return null;
                return { x1, x2, y, color: line.color, label: line.type || "BOS" };
            } catch(e) { return null; }
        }).filter(l => l !== null);
    }
    updateAllViews() { if(this._paneViews[0] && this._paneViews[0].update) this._paneViews[0].update(); }
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
  const bosPrimitiveRef = useRef(null); 
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

  // 🔥 100% FIX: Removed all getCandleStartTime math. 
  // Trust the API's database time completely.
  const processCandles = useCallback((rawData) => {
      if (!Array.isArray(rawData)) return [];
      return rawData
          .filter(d => d.open != null && d.close != null && d.time != null) 
          .map(d => {
              let seconds = Number(d.time);
              if (seconds > 2000000000) seconds = Math.floor(seconds / 1000);
              return {
                  time: seconds,
                  open: parseFloat(d.open),
                  high: parseFloat(d.high),
                  low: parseFloat(d.low),
                  close: parseFloat(d.close)
              };
          })
          .filter(d => !isNaN(d.open) && !isNaN(d.close) && d.open > 0) 
          .sort((a, b) => a.time - b.time)
          .filter((v, i, a) => a.findIndex(t => t.time === v.time) === i); 
  }, []);

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
    
    const boxPrimitive = new BoxPrimitive();
    newSeries.attachPrimitive(boxPrimitive);
    boxPrimitiveRef.current = boxPrimitive;

    const bosPrimitive = new BOSPrimitive();
    newSeries.attachPrimitive(bosPrimitive);
    bosPrimitiveRef.current = bosPrimitive;

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
          
          const newValidData = processCandles(res.data);
          if (newValidData.length === 0) return;

          const combinedData = [...newValidData, ...allDataRef.current]
              .filter((v, i, a) => a.findIndex(t => (t.time === v.time)) === i);

          allDataRef.current = combinedData;
          if (candleSeriesRef.current) candleSeriesRef.current.setData(combinedData);

      } catch (err) { } 
      finally { isLoadingRef.current = false; }
  };

  useEffect(() => {
    if (!isChartReady.current) return;

    const loadInitialHistory = async () => {
      try {
        const res = await axios.get(`${API_URL}/api/candles/${timeframe}`, {
            params: { limit: 1000, timestamp: Date.now() }
        });
        
        const validData = processCandles(res.data);

        if (isChartReady.current && candleSeriesRef.current) {
            allDataRef.current = validData;
            candleSeriesRef.current.setData(validData);
            if (validData.length > 0) currentBarRef.current = validData[validData.length - 1];

            // 🔥 FORCE CHART TO RE-SCALE PROPERLY WHEN TIMEFRAME CHANGES
            setTimeout(() => {
                if (chartRef.current) chartRef.current.timeScale().fitContent();
            }, 50);
        }
      } catch (err) {} 
    };
    loadInitialHistory();
  }, [timeframe, processCandles]);

  // --- 🎨 RENDER VISUALS (OBS & BOS) ---
  useEffect(() => {
    let animationFrameId;
    const renderLoop = () => {
      if (!isChartReady.current) return; 

      if (candleSeriesRef.current && latestCandleRef.current) {
        candleSeriesRef.current.update(latestCandleRef.current);
        currentBarRef.current = latestCandleRef.current;
        latestCandleRef.current = null;
      }

      if (chartRef.current && candleSeriesRef.current) {
          if (visuals?.smc_zones && boxPrimitiveRef.current) {
              boxPrimitiveRef.current.setData(visuals.smc_zones, candleSeriesRef.current, chartRef.current.timeScale());
          }
          if (visuals?.bos_lines && bosPrimitiveRef.current) {
              bosPrimitiveRef.current.setData(visuals.bos_lines, candleSeriesRef.current, chartRef.current.timeScale());
          }
      }

      animationFrameId = requestAnimationFrame(renderLoop);
    };
    renderLoop();
    return () => cancelAnimationFrame(animationFrameId);
  }, [visuals]);

  useEffect(() => {
      const fetchNews = async () => {
          try {
              const res = await axios.get(`${API_URL}/api/news`, { params: { limit: 100 } });
              if (Array.isArray(res.data)) setNewsData(res.data.map(n => ({...n, time: n.time})));
          } catch (err) {}
      };
      fetchNews();
  }, []);

  useEffect(() => {
    if (newsData.length > 0 && isChartReady.current && candleSeriesRef.current) {
        try {
            const markers = newsData.filter(n => n.time).map(n => {
                let seconds = Number(n.time);
                if (seconds > 2000000000) seconds = Math.floor(seconds / 1000);
                return {
                    time: seconds,
                    position: 'aboveBar',
                    color: n.impact === 'High' ? '#ef5350' : '#ffa726',
                    shape: 'arrowDown',
                    text: n.impact === 'High' ? `🚩 ${n.event}` : '', 
                    size: n.impact === 'High' ? 2 : 1,
                };
            }).sort((a, b) => a.time - b.time); 
            
            if (candleSeriesRef.current && typeof candleSeriesRef.current.setMarkers === 'function') {
                candleSeriesRef.current.setMarkers(markers);
            }
        } catch (e) { }
    }
  }, [newsData]);

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

  useEffect(() => {
    const socket = io(API_URL, { transports: ['polling'], path: '/socket.io/' });
    socket.on('connect', () => setConnectionStatus('Connected'));
    socket.on('disconnect', () => setConnectionStatus('Disconnected'));
    socket.on('price-update', (data) => {
        if (!isChartReady.current || !currentBarRef.current) return; 
        const price = parseFloat(data.close || data.c);
        
        if (isNaN(price) || price <= 0) return; 
        
        // 🔥 FIX: Live socket ticks no longer create chaotic new candles. 
        // We only update the current active database candle.
        const lastCandle = allDataRef.current[allDataRef.current.length - 1];
        
        if (lastCandle) {
            const updatedCandle = { 
                ...lastCandle, 
                high: Math.max(lastCandle.high, price), 
                low: Math.min(lastCandle.low, price), 
                close: price 
            };
            allDataRef.current[allDataRef.current.length - 1] = updatedCandle;
            latestCandleRef.current = updatedCandle;
        }
    });
    return () => socket.disconnect();
  }, []); // Removed timeframe dependency so it doesn't refresh unncecessarily

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