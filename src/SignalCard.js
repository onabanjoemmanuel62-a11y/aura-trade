import React, { useState, useEffect } from 'react';
import { TrendingUp, Activity, RefreshCw, History, AlertTriangle, Clock } from 'lucide-react';
import axios from 'axios';

const SignalCard = ({ chartData = [] }) => {
  // --- STATE ---
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(false);
  const [newsCountdown, setNewsCountdown] = useState('--:--:--');

  // --- 1. THE BRAIN: Fetch Analysis (Preserved) ---
  const fetchAnalysis = async () => {
    // Safety check
    if (!chartData || chartData.length < 30) return;

    setLoading(true);
    try {
      // 4. API CALL: Send the last 50 candles
      const res = await axios.post('http://localhost:5000/api/analyze', {
        candles: chartData.slice(-50) 
      });

      if (res.data) {
          setAnalysis(res.data);
      }
    } catch (err) {
      console.error("AI Analysis Failed:", err);
    } finally {
      setLoading(false);
    }
  };

  // --- 2. LIVE TRIGGERS (Preserved) ---
  useEffect(() => {
      if (chartData.length > 0 && !analysis) fetchAnalysis();
      const interval = setInterval(() => { if (chartData.length > 0) fetchAnalysis(); }, 60000);
      return () => clearInterval(interval);
  }, [chartData.length]);

  // --- 3. NEWS TIMER LOGIC (New Feature) ---
  useEffect(() => {
    // Mock target time for UX demonstration (In real app, fetch from /api/news)
    const targetDate = new Date();
    targetDate.setHours(targetDate.getHours() + 2); // Fake "Next News" in 2 hours

    const timer = setInterval(() => {
      const now = new Date();
      const diff = targetDate - now;

      if (diff <= 0) {
        setNewsCountdown("00:00:00");
        return;
      }

      const hours = Math.floor((diff / (1000 * 60 * 60)) % 24);
      const minutes = Math.floor((diff / (1000 * 60)) % 60);
      const seconds = Math.floor((diff / 1000) % 60);

      setNewsCountdown(`${hours}h ${minutes}m ${seconds}s`);
    }, 1000);

    return () => clearInterval(timer);
  }, []);

  // --- HELPERS & VISUAL MATH ---
  const getSignalColor = (signal) => {
      if (!signal) return '#9ca3af';
      const s = signal.toUpperCase();
      if (s.includes('BUY')) return '#00E676'; // Bright Green
      if (s.includes('SELL')) return '#FF1744'; // Bright Red
      return '#FFC107'; // Amber/Wait
  };

  const signalColor = getSignalColor(analysis?.signal);
  const confidence = analysis?.confidence || 0;
  
  // SVG Gauge Math
  const radius = 36; // Size of the ring
  const circumference = 2 * Math.PI * radius;
  const strokeDashoffset = circumference - (confidence / 100) * circumference;

  return (
    <div style={{
      backgroundColor: 'rgba(21, 25, 32, 0.8)', // Slightly darker for contrast
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(255, 255, 255, 0.08)',
      borderRadius: '16px',
      padding: '20px',
      height: '100%',
      color: '#e1e3e6',
      display: 'flex',
      flexDirection: 'column',
      gap: '16px',
      boxShadow: '0 4px 20px rgba(0,0,0,0.2)'
    }}>
      
      {/* HEADER */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ fontSize: '15px', margin: 0, color: '#9ca3af', display: 'flex', alignItems: 'center', gap: '8px', fontWeight: '600' }}>
             <Activity size={16} color={signalColor} /> AI BRAIN
        </h2>
        <button 
            onClick={fetchAnalysis}
            disabled={loading}
            style={{ 
                background: 'rgba(255,255,255,0.05)', border: 'none', 
                color: '#9ca3af', borderRadius: '6px', cursor: 'pointer', padding: '6px',
                display: 'flex', alignItems: 'center', justifyContent: 'center'
            }}
        >
            <RefreshCw size={14} className={loading ? "spin" : ""} />
            <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }`}</style>
        </button>
      </div>

      {loading && !analysis ? (
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: '8px', color: '#6b7280' }}>
              <RefreshCw size={20} className="spin" />
              <span style={{fontSize: '12px'}}>Simulating Outcomes...</span>
          </div>
      ) : (
        <>
            {/* 1. WIN RATE METER (Circular Gauge) */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: 'linear-gradient(145deg, rgba(255,255,255,0.03) 0%, rgba(0,0,0,0.2) 100%)', padding: '15px', borderRadius: '12px', border: '1px solid rgba(255,255,255,0.05)' }}>
                <div style={{ display: 'flex', flexDirection: 'column' }}>
                    <span style={{ fontSize: '11px', color: '#9ca3af', textTransform: 'uppercase', letterSpacing: '0.5px' }}>Probability</span>
                    <span style={{ fontSize: '28px', fontWeight: '900', color: signalColor, lineHeight: '1' }}>
                        {confidence}%
                    </span>
                    <span style={{ fontSize: '10px', color: signalColor, opacity: 0.8, marginTop: '4px' }}>
                        {analysis?.signal || 'NEUTRAL'}
                    </span>
                </div>
                
                {/* SVG Gauge */}
                <div style={{ position: 'relative', width: '80px', height: '80px' }}>
                    <svg width="80" height="80" style={{ transform: 'rotate(-90deg)' }}>
                        {/* Background Ring */}
                        <circle cx="40" cy="40" r={radius} stroke="#374357" strokeWidth="6" fill="transparent" opacity="0.3" />
                        {/* Value Ring */}
                        <circle 
                            cx="40" cy="40" r={radius} 
                            stroke={signalColor} strokeWidth="6" fill="transparent" 
                            strokeDasharray={circumference} 
                            strokeDashoffset={strokeDashoffset} 
                            strokeLinecap="round"
                            style={{ transition: 'stroke-dashoffset 1s ease-in-out' }}
                        />
                    </svg>
                    {/* Center Icon */}
                    <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%, -50%)', color: '#FFF' }}>
                        <TrendingUp size={18} />
                    </div>
                </div>
            </div>

            {/* 2. HISTORICAL REFERENCE (The "Why") */}
            <div style={{ background: 'rgba(255,255,255,0.02)', padding: '12px', borderRadius: '8px', borderLeft: '3px solid #7C4DFF' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px', color: '#7C4DFF', fontSize: '11px', fontWeight: 'bold' }}>
                    <History size={14} /> FRACTAL MATCH
                </div>
                <div style={{ fontSize: '12px', color: '#d1d5db', lineHeight: '1.4' }}>
                    "Pattern matches the <b>Liquidity Sweep</b> seen in <span style={{color: '#FFF'}}>Oct 2019</span>. Expect volatility."
                </div>
            </div>

            {/* 3. IMPACT ALERT (News Timer) */}
            <div style={{ background: 'rgba(239, 83, 80, 0.1)', padding: '12px', borderRadius: '8px', border: '1px solid rgba(239, 83, 80, 0.2)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#ef5350', fontSize: '11px', fontWeight: 'bold' }}>
                        <AlertTriangle size={14} /> NEWS ALERT
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '4px', color: '#ef5350', fontSize: '12px', fontFamily: 'monospace', fontWeight: 'bold' }}>
                        <Clock size={12} /> {newsCountdown}
                    </div>
                </div>
                <div style={{ fontSize: '11px', color: '#ff8a80', marginTop: '4px' }}>
                    Upcoming: <b>CPI Data Release</b>
                </div>
            </div>

            {/* 4. TRADE SETUP GRID (Compact) */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '8px', marginTop: 'auto' }}>
                <div style={{ background: 'rgba(0,0,0,0.3)', padding: '8px', borderRadius: '6px', textAlign: 'center' }}>
                    <div style={{ fontSize: '9px', color: '#9ca3af', marginBottom: '2px' }}>ENTRY</div>
                    <div style={{ fontSize: '12px', fontWeight: 'bold' }}>{analysis?.entry || '--'}</div>
                </div>
                <div style={{ background: 'rgba(239, 83, 80, 0.15)', padding: '8px', borderRadius: '6px', textAlign: 'center', border: '1px solid rgba(239, 83, 80, 0.3)' }}>
                    <div style={{ fontSize: '9px', color: '#ef5350', marginBottom: '2px' }}>STOP</div>
                    <div style={{ fontSize: '12px', fontWeight: 'bold', color: '#ef5350' }}>{analysis?.stopLoss || '--'}</div>
                </div>
                <div style={{ background: 'rgba(0, 230, 118, 0.15)', padding: '8px', borderRadius: '6px', textAlign: 'center', border: '1px solid rgba(0, 230, 118, 0.3)' }}>
                    <div style={{ fontSize: '9px', color: '#00e676', marginBottom: '2px' }}>TARGET</div>
                    <div style={{ fontSize: '12px', fontWeight: 'bold', color: '#00e676' }}>{analysis?.takeProfit || '--'}</div>
                </div>
            </div>

        </>
      )}
    </div>
  );
};

export default SignalCard;