import React, { useState, useEffect } from 'react';
import { TrendingUp, Activity, RefreshCw, History, AlertTriangle, Clock } from 'lucide-react';
import axios from 'axios';

// ☁️ LIVE SERVER ADDRESS
const API_URL = 'https://aura-trade.onrender.com';

const SignalCard = ({ chartData = [] }) => {
  // --- STATE ---
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(false);
  
  // 📰 Real News State
  const [nextNews, setNextNews] = useState(null); 
  const [newsCountdown, setNewsCountdown] = useState('--:--:--');

  // --- 1. THE BRAIN: Fetch Analysis (Phase 2 Connected) ---
  const fetchAnalysis = async (newsEvent = null) => {
    setLoading(true);
    try {
      console.log("🧠 AI Brain: Requesting Analysis...");
      
      // Determine what to ask the backend
      // If we found 'High Impact News' (newsEvent), we tell the backend to analyze IT.
      // Otherwise, we just ask for standard technical analysis.
      const payload = {
        timeframe: '1h',
        currency: 'USD',
        eventName: newsEvent ? newsEvent.event : null // <--- KEY: Triggers Historical Analysis
      };

      const res = await axios.post(`${API_URL}/api/analyze`, payload);
      
      if (res.data) {
          console.log("🧠 Brain Result:", res.data);
          setAnalysis(res.data);
      }
    } catch (err) {
      console.error("❌ AI Analysis Failed:", err);
    } finally {
      setLoading(false);
    }
  };

  // --- 2. THE FILTER: Find "Sniper" News Only ---
  const fetchDailyNews = async () => {
      try {
          const res = await axios.get(`${API_URL}/api/news`);
          const today = new Date().toDateString(); 

          // 🚨 THE SNIPER FILTER 🚨
          // Find the most critical UPCOMING event for today
          const criticalEvent = res.data.find(n => {
              const newsDate = new Date(n.time * 1000).toDateString(); // Ensure n.time is handled correctly (sec vs ms)
              const eventTime = new Date(n.time * 1000); // MongoDB usually stores seconds
              const isFuture = eventTime > new Date();
              
              return (
                  newsDate === today &&       // Must be today
                  n.impact === 'High' &&      // Must be High Impact
                  n.currency === 'USD' &&     // Must be USD
                  isFuture                    // Must be in future
              );
          });

          if (criticalEvent) {
            console.log("🚨 Sniper Alert:", criticalEvent.event);
            setNextNews(criticalEvent);
            // If we found news, immediately run analysis on it!
            fetchAnalysis(criticalEvent); 
          } else {
            setNextNews(null);
            // If no news, run standard analysis
            fetchAnalysis(null);
          }

      } catch (err) {
          console.error("News Fetch Failed:", err);
      }
  };

  // --- 3. COUNTDOWN TIMER ---
  useEffect(() => {
    if (!nextNews) return;

    // Handle timestamp conversion (Seconds vs Milliseconds)
    const eventTime = nextNews.time > 2000000000 ? nextNews.time : nextNews.time * 1000;
    const targetDate = new Date(eventTime);
    
    const timer = setInterval(() => {
      const now = new Date();
      const diff = targetDate - now;

      if (diff <= 0) {
        setNextNews(null); 
        setNewsCountdown("");
        fetchAnalysis(null); // Re-run analysis now that news passed
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
      // On Mount: Check News -> Then Check Analysis
      fetchDailyNews();
      
      const interval = setInterval(() => { 
          fetchDailyNews(); 
      }, 60000); // Re-check every minute
      
      return () => clearInterval(interval);
  }, []); // Run once on mount

  // --- HELPERS ---
  const getSignalColor = (signal) => {
      if (!signal) return '#9ca3af';
      const s = signal.toUpperCase();
      if (s.includes('BUY') || s.includes('STRONG') || s.includes('FOLLOW')) return '#00E676'; 
      if (s.includes('SELL') || s.includes('WEAK') || s.includes('INVERSE')) return '#FF1744'; 
      return '#FFC107'; 
  };

  const signalColor = getSignalColor(analysis?.signal);
  const confidence = analysis?.confidence || 0;
  
  // SVG Gauge Math
  const radius = 36;
  const circumference = 2 * Math.PI * radius;
  const strokeDashoffset = circumference - (confidence / 100) * circumference;

  // 🔴 VISUAL CUE: Border turns RED if high impact news is coming
  const cardBorder = nextNews ? '1px solid #ef5350' : '1px solid rgba(255, 255, 255, 0.08)';

  return (
    <div style={{
      backgroundColor: 'rgba(21, 25, 32, 0.8)', 
      backdropFilter: 'blur(12px)',
      border: cardBorder, // <--- DYNAMIC BORDER
      borderRadius: '16px',
      padding: '20px',
      height: '100%',
      color: '#e1e3e6',
      display: 'flex',
      flexDirection: 'column',
      gap: '16px',
      boxShadow: nextNews ? '0 0 15px rgba(239, 83, 80, 0.2)' : '0 4px 20px rgba(0,0,0,0.2)',
      transition: 'border 0.3s ease'
    }}>
      
      {/* HEADER */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ fontSize: '15px', margin: 0, color: '#9ca3af', display: 'flex', alignItems: 'center', gap: '8px', fontWeight: '600' }}>
             <Activity size={16} color={signalColor} /> AI BRAIN
        </h2>
        <button 
            onClick={() => fetchAnalysis(nextNews)}
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
                    <span style={{ fontSize: '10px', color: signalColor, opacity: 0.8, marginTop: '4px', fontWeight: 'bold' }}>
                        {confidence > 0 ? (analysis?.signal || 'NEUTRAL') : 'NO SIGNAL'}
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

            {/* 2. LOGIC / REASONING (Expanded & Scrollable) */}
            <div style={{ background: 'rgba(255,255,255,0.02)', padding: '12px', borderRadius: '8px', borderLeft: `3px solid ${signalColor}` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px', color: signalColor, fontSize: '11px', fontWeight: 'bold' }}>
                    <History size={14} /> STRATEGY LOGIC
                </div>
                <div style={{ 
                    fontSize: '12px', 
                    color: '#d1d5db', 
                    lineHeight: '1.5',
                    maxHeight: '100px', // Allow text to grow
                    overflowY: 'auto',  // Scroll if text is huge
                    whiteSpace: 'pre-wrap' // Preserve formatting
                }}>
                    {analysis?.reason ? analysis.reason : "Scanning market structure..."}
                </div>
            </div>

            {/* 3. IMPACT ALERT */}
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

            {/* 4. CONTEXT / TREND */}
            {confidence > 0 && analysis.trend && (
                <div style={{ display: 'flex', gap: '8px', marginTop: 'auto' }}>
                     <div style={{ flex: 1, background: 'rgba(255,255,255,0.05)', padding: '8px', borderRadius: '6px', textAlign: 'center' }}>
                         <div style={{ fontSize: '9px', color: '#9ca3af' }}>MARKET BIAS</div>
                         <div style={{ fontSize: '12px', fontWeight: 'bold', color: '#fff' }}>{analysis.trend}</div>
                     </div>
                     {analysis.pattern && (
                        <div style={{ flex: 1, background: 'rgba(255,255,255,0.05)', padding: '8px', borderRadius: '6px', textAlign: 'center' }}>
                            <div style={{ fontSize: '9px', color: '#9ca3af' }}>PATTERN</div>
                            <div style={{ fontSize: '12px', fontWeight: 'bold', color: '#fff' }}>{analysis.pattern}</div>
                        </div>
                     )}
                </div>
            )}

            {/* 5. QUIET STATE */}
            {confidence === 0 && (
                <div style={{ 
                    marginTop: 'auto', 
                    padding: '15px', 
                    textAlign: 'center', 
                    border: '1px dashed #374357', 
                    borderRadius: '8px',
                    color: '#9ca3af',
                    fontSize: '12px',
                    fontStyle: 'italic'
                }}>
                    "Market is Quiet. Waiting for Setup..."
                </div>
            )}

        </>
      )}
    </div>
  );
};

export default SignalCard;  