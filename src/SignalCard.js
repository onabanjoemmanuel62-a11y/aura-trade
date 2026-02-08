import React, { useState, useEffect } from 'react';
import { TrendingUp, Activity, RefreshCw, History, AlertTriangle, Clock } from 'lucide-react';
import axios from 'axios';

const SignalCard = ({ chartData = [] }) => {
  // --- STATE ---
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(false);
  
  // 📰 NEW: Real News State
  const [nextNews, setNextNews] = useState(null); 
  const [newsCountdown, setNewsCountdown] = useState('--:--:--');

  // --- 1. THE BRAIN: Fetch Analysis (Preserved) ---
  const fetchAnalysis = async () => {
    if (!chartData || chartData.length < 30) return;

    setLoading(true);
    try {
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

  // --- 2. THE FILTER: Find "Sniper" News Only (NEW) ---
  const fetchDailyNews = async () => {
      try {
          const res = await axios.get('http://localhost:5000/api/news');
          const today = new Date().toDateString(); // e.g. "Fri Feb 07 2026"

          // 🚨 THE SNIPER FILTER 🚨
          // We find the *next* critical event happening today.
          const criticalEvent = res.data.find(n => {
              const newsDate = new Date(n.time).toDateString();
              return (
                  newsDate === today &&       // Must be today
                  n.impact === 'High' &&      // Must be High Impact (Red Folder)
                  n.currency === 'USD' &&     // Must be USD (affects Gold)
                  new Date(n.time) > new Date() // Must be in the future
              );
          });

          setNextNews(criticalEvent || null);

      } catch (err) {
          console.error("News Fetch Failed:", err);
      }
  };

  // --- 3. COUNTDOWN TIMER (UPDATED) ---
  useEffect(() => {
    if (!nextNews) return;

    const targetDate = new Date(nextNews.time);
    
    const timer = setInterval(() => {
      const now = new Date();
      const diff = targetDate - now;

      // If news time passed, hide the alert
      if (diff <= 0) {
        setNextNews(null); 
        setNewsCountdown("");
        return;
      }

      const h = Math.floor((diff / (1000 * 60 * 60)) % 24);
      const m = Math.floor((diff / (1000 * 60)) % 60);
      const s = Math.floor((diff / 1000) % 60);

      setNewsCountdown(`${h}h ${m}m ${s}s`);
    }, 1000);

    return () => clearInterval(timer);
  }, [nextNews]);

  // --- TRIGGERS ---
  useEffect(() => {
      fetchDailyNews(); // Check news immediately on load
      
      if (chartData.length > 0 && !analysis) fetchAnalysis();
      
      const interval = setInterval(() => { 
          if (chartData.length > 0) fetchAnalysis();
          fetchDailyNews(); // Re-check news every minute
      }, 60000);
      
      return () => clearInterval(interval);
  }, [chartData.length]);

  // --- HELPERS ---
  const getSignalColor = (signal) => {
      if (!signal) return '#9ca3af';
      const s = signal.toUpperCase();
      if (s.includes('BUY')) return '#00E676'; 
      if (s.includes('SELL')) return '#FF1744'; 
      return '#FFC107'; 
  };

  const signalColor = getSignalColor(analysis?.signal);
  const confidence = analysis?.confidence || 0;
  
  // SVG Gauge Math
  const radius = 36;
  const circumference = 2 * Math.PI * radius;
  const strokeDashoffset = circumference - (confidence / 100) * circumference;

  return (
    <div style={{
      backgroundColor: 'rgba(21, 25, 32, 0.8)', 
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
            {/* 1. WIN RATE METER */}
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
                        <circle cx="40" cy="40" r={radius} stroke="#374357" strokeWidth="6" fill="transparent" opacity="0.3" />
                        <circle 
                            cx="40" cy="40" r={radius} 
                            stroke={signalColor} strokeWidth="6" fill="transparent" 
                            strokeDasharray={circumference} 
                            strokeDashoffset={strokeDashoffset} 
                            strokeLinecap="round"
                            style={{ transition: 'stroke-dashoffset 1s ease-in-out' }}
                        />
                    </svg>
                    <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%, -50%)', color: '#FFF' }}>
                        <TrendingUp size={18} />
                    </div>
                </div>
            </div>

            {/* 2. HISTORICAL REFERENCE */}
            <div style={{ background: 'rgba(255,255,255,0.02)', padding: '12px', borderRadius: '8px', borderLeft: '3px solid #7C4DFF' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px', color: '#7C4DFF', fontSize: '11px', fontWeight: 'bold' }}>
                    <History size={14} /> FRACTAL MATCH
                </div>
                <div style={{ fontSize: '12px', color: '#d1d5db', lineHeight: '1.4' }}>
                    "Pattern matches the <b>Liquidity Sweep</b> seen in <span style={{color: '#FFF'}}>Oct 2019</span>. Expect volatility."
                </div>
            </div>

            {/* 3. IMPACT ALERT (Dynamic Visibility) */}
            {nextNews && (
                <div style={{ background: 'rgba(239, 83, 80, 0.1)', padding: '12px', borderRadius: '8px', border: '1px solid rgba(239, 83, 80, 0.2)', animation: 'pulse 2s infinite' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#ef5350', fontSize: '11px', fontWeight: 'bold' }}>
                            <AlertTriangle size={14} /> NEWS ALERT
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '4px', color: '#ef5350', fontSize: '12px', fontFamily: 'monospace', fontWeight: 'bold' }}>
                            <Clock size={12} /> {newsCountdown}
                        </div>
                    </div>
                    <div style={{ fontSize: '11px', color: '#ff8a80', marginTop: '4px' }}>
                        Upcoming: <b>{nextNews.title || nextNews.event || "High Impact News"}</b>
                    </div>
                    <style>{`@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(239, 83, 80, 0.4); } 70% { box-shadow: 0 0 0 10px rgba(239, 83, 80, 0); } 100% { box-shadow: 0 0 0 0 rgba(239, 83, 80, 0); } }`}</style>
                </div>
            )}

            {/* 4. TRADE SETUP GRID */}
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