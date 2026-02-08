import React, { useState, useEffect } from 'react';
import { TrendingUp, Activity, RefreshCw } from 'lucide-react';
import axios from 'axios';

// 1. PROPS: We accept 'chartData' (Array of candles)
const SignalCard = ({ chartData = [] }) => {
  
  // 2. STATE
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(false);

  // 3. THE BRAIN: Talks to the Backend
  const fetchAnalysis = async () => {
    // Safety: Need enough data to see patterns
    if (!chartData || chartData.length < 30) {
        return; 
    }

    setLoading(true);
    try {
      console.log("🧠 AI: Scanning Market Structure...");
      
      // 4. API CALL: Send the last 50 candles (Standard Context Window)
      const res = await axios.post('http://localhost:5000/api/analyze', {
        candles: chartData.slice(-50) 
      });

      // 5. UPDATE UI: Set the new thought process
      if (res.data) {
          setAnalysis(res.data);
      }
    } catch (err) {
      console.error("AI Analysis Failed:", err);
    } finally {
      setLoading(false);
    }
  };

  // 6. LIVE TRIGGERS (The "TV Signal" Connection)
  useEffect(() => {
      // Trigger 1: Analyze immediately if we have data and no analysis
      if (chartData.length > 0 && !analysis) {
          fetchAnalysis();
      }

      // Trigger 2: Re-Analyze automatically every 60 seconds
      const interval = setInterval(() => {
          if (chartData.length > 0) fetchAnalysis();
      }, 60000);

      return () => clearInterval(interval);
  }, [chartData.length]); // Also re-run if a NEW candle is added

  // Helper for Colors
  const getSignalColor = (signal) => {
      if (!signal) return '#9ca3af';
      const s = signal.toUpperCase();
      if (s === 'BUY' || s === 'STRONG BUY') return '#26a69a'; // Green
      if (s === 'SELL' || s === 'STRONG SELL') return '#ef5350'; // Red
      return '#fbc02d'; // Neutral/Wait
  };

  const signalColor = getSignalColor(analysis?.signal);

  return (
    <div style={{
      backgroundColor: 'rgba(21, 25, 32, 0.7)',
      backdropFilter: 'blur(10px)',
      border: '1px solid rgba(255, 255, 255, 0.1)',
      borderRadius: '12px',
      padding: '20px',
      height: '100%',
      color: '#e1e3e6',
      display: 'flex',
      flexDirection: 'column',
      gap: '20px',
      position: 'relative'
    }}>
      
      {/* HEADER */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ fontSize: '18px', margin: 0, color: '#9ca3af', display: 'flex', alignItems: 'center', gap: '8px' }}>
             <Activity size={18} /> AI Signal
        </h2>
        
        {/* REFRESH BUTTON (Manual Override) */}
        <button 
            onClick={fetchAnalysis}
            disabled={loading}
            style={{ 
                background: 'transparent', border: '1px solid #374357', 
                color: '#9ca3af', borderRadius: '4px', cursor: 'pointer', padding: '4px',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                transition: 'all 0.2s'
            }}
            onMouseOver={(e) => e.currentTarget.style.borderColor = '#26a69a'}
            onMouseOut={(e) => e.currentTarget.style.borderColor = '#374357'}
        >
            <RefreshCw size={14} className={loading ? "spin" : ""} />
            <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }`}</style>
        </button>
      </div>

      {loading && !analysis ? (
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: '10px', color: '#9ca3af' }}>
              <RefreshCw size={24} className="spin" />
              <span style={{fontSize: '12px'}}>Scanning Price Action...</span>
          </div>
      ) : (
        <>
            {/* SIGNAL BADGE */}
            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', flexDirection: 'column', gap: '10px' }}>
                <div style={{ 
                    backgroundColor: `${signalColor}33`, 
                    color: signalColor, 
                    padding: '8px 24px', 
                    borderRadius: '30px',
                    fontWeight: '900',
                    fontSize: '20px',
                    border: `2px solid ${signalColor}`,
                    letterSpacing: '1px',
                    boxShadow: `0 0 15px ${signalColor}20`
                }}>
                    {analysis ? analysis.signal.toUpperCase() : "WAITING..."}
                </div>
            </div>

            {/* CONFIDENCE METER */}
            <div style={{ display: 'flex', justifyContent: 'center', margin: '5px 0' }}>
                <div style={{ 
                width: '100px', height: '100px', borderRadius: '50%', 
                border: `4px solid ${signalColor}`, 
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                boxShadow: `0 0 20px ${signalColor}40`,
                background: 'rgba(0,0,0,0.2)'
                }}>
                <span style={{ fontSize: '24px', fontWeight: 'bold' }}>
                    {analysis?.confidence || 0}%
                </span>
                <span style={{ fontSize: '10px', color: '#9ca3af', textTransform: 'uppercase' }}>Confidence</span>
                </div>
            </div>

            {/* TRADE SETUP */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '15px' }}>
                <div style={{ background: 'rgba(255,255,255,0.05)', padding: '10px', borderRadius: '8px' }}>
                    <div style={{ fontSize: '11px', color: '#9ca3af' }}>ENTRY</div>
                    <div style={{ fontSize: '15px', fontWeight: 'bold' }}>{analysis?.entry || '...'}</div>
                </div>
                <div style={{ background: 'rgba(255,255,255,0.05)', padding: '10px', borderRadius: '8px' }}>
                    <div style={{ fontSize: '11px', color: '#ef5350' }}>STOP LOSS</div>
                    <div style={{ fontSize: '15px', fontWeight: 'bold', color: '#ef5350' }}>{analysis?.stopLoss || '...'}</div>
                </div>
                <div style={{ background: 'rgba(255,255,255,0.05)', padding: '10px', borderRadius: '8px', gridColumn: 'span 2' }}>
                    <div style={{ fontSize: '11px', color: '#26a69a' }}>TAKE PROFIT</div>
                    <div style={{ fontSize: '15px', fontWeight: 'bold', color: '#26a69a' }}>{analysis?.takeProfit || '...'}</div>
                </div>
            </div>

            {/* REASONING */}
            <div style={{ marginTop: 'auto' }}>
                <h3 style={{ fontSize: '13px', marginBottom: '8px', color: '#9ca3af', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <TrendingUp size={14} /> AI Reasoning
                </h3>
                <ul style={{ paddingLeft: '20px', fontSize: '12px', color: '#d1d5db', lineHeight: '1.5' }}>
                    {analysis?.reasoning && analysis.reasoning.length > 0 ? (
                        analysis.reasoning.map((item, index) => (
                            <li key={index} style={{ marginBottom: '4px' }}>{item}</li>
                        ))
                    ) : (
                        <li>Waiting for volatility...</li>
                    )}
                </ul>
            </div>
        </>
      )}
    </div>
  );
};

export default SignalCard; 