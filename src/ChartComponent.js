import React, { useEffect, useRef, useState } from 'react';
import { createChart, ColorType, CandlestickSeries } from 'lightweight-charts'; // 👈 Import Series Class
import axios from 'axios';
import io from 'socket.io-client';

// ☁️ LIVE CLOUD SERVER ADDRESS
const API_URL = 'https://aura-trade-v1.onrender.com';

const ChartComponent = ({ levels, visuals, tradeSetup }) => {
    const chartContainerRef = useRef(null);
    const chartRef = useRef(null);
    const candleSeriesRef = useRef(null);
    
    // State
    const [timeframe, setTimeframe] = useState('1h');
    const [connectionStatus, setConnectionStatus] = useState('Connecting...');
    const isChartReady = useRef(false);

    // --- 1. INITIALIZE CHART ---
    useEffect(() => {
        if (!chartContainerRef.current) return;

        // Create Chart
        const chart = createChart(chartContainerRef.current, {
            layout: { 
                background: { type: ColorType.Solid, color: '#161A25' }, 
                textColor: '#D9D9D9' 
            },
            grid: { 
                vertLines: { color: 'rgba(42, 46, 57, 0.1)' }, 
                horzLines: { color: 'rgba(42, 46, 57, 0.1)' }, 
            },
            width: chartContainerRef.current.clientWidth,
            height: 500,
            timeScale: { 
                timeVisible: true, 
                secondsVisible: false,
                rightOffset: 5,
            },
        });

        // ✅ FIX FOR VERSION 5: Use addSeries(CandlestickSeries, options)
        const candleSeries = chart.addSeries(CandlestickSeries, {
            upColor: '#089981', downColor: '#F23645', 
            borderVisible: false, wickUpColor: '#089981', wickDownColor: '#F23645'
        });

        chartRef.current = chart;
        candleSeriesRef.current = candleSeries;
        isChartReady.current = true;

        // Handle Resize
        const handleResize = () => {
            if (chartRef.current) {
                chartRef.current.applyOptions({ width: chartContainerRef.current.clientWidth });
            }
        };
        window.addEventListener('resize', handleResize);

        return () => {
            window.removeEventListener('resize', handleResize);
            chart.remove();
            isChartReady.current = false;
        };
    }, []);

    // --- 2. LOAD INITIAL HISTORY ---
    useEffect(() => {
        const loadHistory = async () => {
            try {
                const res = await axios.get(`${API_URL}/api/candles/${timeframe}`, {
                    params: { limit: 1000, timestamp: Date.now() }
                });
                
                if (res.data && Array.isArray(res.data) && candleSeriesRef.current) {
                    const sortedData = res.data
                        .map(d => ({
                            time: d.time,
                            open: parseFloat(d.open),
                            high: parseFloat(d.high),
                            low: parseFloat(d.low),
                            close: parseFloat(d.close)
                        }))
                        .sort((a, b) => a.time - b.time); 
                    
                    const uniqueData = [...new Map(sortedData.map(item => [item.time, item])).values()];
                    
                    candleSeriesRef.current.setData(uniqueData);
                }
            } catch (err) {
                console.error("History Load Failed:", err);
            }
        };

        if (isChartReady.current) {
            loadHistory();
        }
    }, [timeframe]);

    // --- 3. LIVE SOCKET UPDATES ---
    useEffect(() => {
        const socket = io(API_URL, { transports: ['polling'], path: '/socket.io/' });

        socket.on('connect', () => setConnectionStatus('Connected'));
        socket.on('disconnect', () => setConnectionStatus('Disconnected'));
        
        socket.on('price-update', (data) => {
            if (candleSeriesRef.current) {
                const price = parseFloat(data.close);
                const time = data.time; 
                
                candleSeriesRef.current.update({
                    time: time,
                    open: parseFloat(data.open),
                    high: parseFloat(data.high),
                    low: parseFloat(data.low),
                    close: price
                });
            }
        });

        return () => socket.disconnect();
    }, []);

    // --- 4. DRAW TRADING LEVELS ---
    useEffect(() => {
        if (!candleSeriesRef.current || !tradeSetup) return;
        
        const createLine = (price, color, title) => {
            candleSeriesRef.current.createPriceLine({
                price: parseFloat(price),
                color: color,
                lineWidth: 2,
                lineStyle: 2, 
                axisLabelVisible: true,
                title: title,
            });
        };

        if (tradeSetup.entry) createLine(tradeSetup.entry, '#2962FF', 'ENTRY');
        if (tradeSetup.take_profit) createLine(tradeSetup.take_profit, '#00E676', 'TP');
        if (tradeSetup.stop_loss) createLine(tradeSetup.stop_loss, '#FF1744', 'SL');

    }, [tradeSetup]);

    return (
        <div style={{ position: 'relative', width: '100%', height: '500px', background: '#161A25', borderRadius: '8px', padding: '10px' }}>
            <div style={{ position: 'absolute', top: 10, left: 10, zIndex: 10, display: 'flex', gap: 10 }}>
                <span style={{ color: connectionStatus === 'Connected' ? '#00E676' : '#FF1744', fontSize: '12px', fontWeight: 'bold' }}>
                    ● {connectionStatus}
                </span>
                <button onClick={() => setTimeframe('1h')} style={{ background: '#333', color: '#fff', border: 'none', padding: '2px 8px', borderRadius: '4px', cursor: 'pointer' }}>1H</button>
                <button onClick={() => setTimeframe('4h')} style={{ background: '#333', color: '#fff', border: 'none', padding: '2px 8px', borderRadius: '4px', cursor: 'pointer' }}>4H</button>
            </div>
            <div ref={chartContainerRef} style={{ width: '100%', height: '100%' }} />
        </div>
    );
};

export default ChartComponent;