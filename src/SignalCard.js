import React, { useState, useEffect } from 'react';
import { TrendingUp, Activity, RefreshCw, History, AlertTriangle, Clock } from 'lucide-react';
import axios from 'axios';

// ☁️ LIVE SERVER ADDRESS
const API_URL = 'https://aura-trade.onrender.com';

const SignalCard = ({ externalData, loading, onRefresh }) => {
  // --- STATE ---
  const [analysis, setAnalysis] = useState(null);
  const [newsLoading, setNewsLoading] = useState(false);
  
  // 📰 Real News State
  const [nextNews, setNextNews] = useState(null); 
  const [newsCountdown, setNewsCountdown] = useState('--:--:--');

  // --- 1. BRIDGE: Sync with App.js ---
  // This is the "synapse" that connects App.js brain to this card
  useEffect(() => {
    if (externalData) {
      setAnalysis(externalData);
    }
  }, [externalData]);

  // --- 2. INTERNAL BRAIN: Fetch Analysis (For News Events Only) ---
  const fetchLocalAnalysis = async (newsEvent = null) => {
    setNewsLoading(true);
    try {
      console.log("🧠 SignalCard: Checking News Impact...");
      
      const payload = {
        timeframe: '1h',
        currency: 'USD',
        eventName: newsEvent ? newsEvent.event : null 
      };

      const res = await axios.post(`${API_URL}/api/analyze`, payload);
      
      if (res.data) {
          console.log("🧠 Brain Result (News):", res.data);
          setAnalysis(res.data);
      }
    } catch (err) {
      console.error("❌ AI Analysis Failed:", err);
    } finally {
      setNewsLoading(false);
    }
  };

  // --- 3. THE FILTER: Find "Sniper" News Only ---
  const fetchDailyNews = async () => {
      try {
          const res = await axios.get(`${API_URL}/api/news`);
          const today = new Date().toDateString(); 

          // 🚨 THE SNIPER FILTER 🚨
          const criticalEvent = res.data.find(n => {
              const newsDate = new Date(n.time * 1000).toDateString();
              const eventTime = new Date(n.time * 1000);
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
            // If news is found, we trigger a specific News Analysis which OVERRIDES the main one
            fetchLocalAnalysis(criticalEvent); 
          } else {
            setNextNews(null);
            // If no news, we rely on the App.js loop (externalData)
          }

      } catch (err) {
          console.error("News Fetch Failed:", err);
      }
  };

  // --- 4. COUNTDOWN TIMER ---
  useEffect(() => {
    if (!nextNews) return;

    const eventTime = nextNews.time > 2000000000 ? nextNews.time : nextNews.time * 1000;
    const targetDate = new Date(eventTime);
    
    const timer = setInterval(() => {
      const now = new Date();
      const diff = targetDate - now;

      if (diff <= 0) {
        setNextNews(null); 
        setNewsCountdown("");
        onRefresh(); // Trigger main app refresh when news passes
        return;
      }

      const h = Math.floor((diff / (1000 * 60 * 60)) % 24);
      const m = Math.floor((diff / (1000 * 60)) % 60);
      const s = Math.floor((diff / 1000) % 60);

      setNewsCountdown(`${h}h ${m}m ${s}s`);
    }, 1000);

    return () => clearInterval(timer);
  }, [nextNews, onRefresh]);

  // --- TRIGGERS ---
  useEffect(() => {
      fetchDailyNews();
      const interval = setInterval(fetchDailyNews, 60000); 
      return () => clearInterval(interval);
  }, []); 

  // --- HELPERS ---
  const getSignalColor = (signal) => {
      if (!signal) return '#9ca3af';
      const s = signal.toUpperCase();
      if (s.includes('BUY') || s.includes('STRONG') || s.includes('FOLLOW')) return '#00E676'; 
      if (s.includes('SELL') || s.includes('WEAK') || s.includes('INVERSE')) return '#FF1744'; 
      return '#FFC107'; 
  };

  const signalColor = getSignalColor(analysis?.signal);
  const confidence = Math.max(0, analysis?.confidence || 0);
  
  // SVG Gauge Math
  const radius = 36;
  const circumference = 2 * Math.PI * radius;
  const strokeDashoffset = circumference - (confidence / 100) * circumference;

  // Visual Loading State (Either App is loading OR Local News check is loading)
  const isBusy = loading || newsLoading;

  const cardBorder = nextNews ? '1px solid #ef5350' : '1px solid rgba(255, 255, 255, 0.08)';

  return (
    <div style={{
      backgroundColor: 'rgba(21, 25, 32, 0.8)', 
      backdropFilter: 'blur(12px)',
      border: cardBorder, 
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
        {/* REFRESH BUTTON: Uses the App.js function */}
        <button 
            onClick={onRefresh}
            disabled={isBusy}
            style={{ 
                background: 'rgba(255,255,255,0.05)', border: 'none', 
                color: '#9ca3af', borderRadius: '6px', cursor: 'pointer', padding: '6px',
                display: 'flex', alignItems: 'center', justifyContent: 'center'
            }}
        >
            <RefreshCw size={14} className={isBusy ? "spin" : ""} />
            <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }`}</style>
        </button>
      </div>

      {isBusy && !analysis ? (
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

            {/* 2. LOGIC / REASONING (Robust Display) */}
            <div style={{ background: 'rgba(255,255,255,0.02)', padding: '12px', borderRadius: '8px', borderLeft: `3px solid ${signalColor}` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px', color: signalColor, fontSize: '11px', fontWeight: 'bold' }}>
                    <History size={14} /> STRATEGY LOGIC
                </div>
                <div style={{ 
                    fontSize: '12px', 
                    color: '#d1d5db', 
                    lineHeight: '1.5',
                    maxHeight: '100px', 
                    overflowY: 'auto', 
                    whiteSpace: 'pre-wrap',
                    fontFamily: 'monospace' // Easier to read list items
                }}>
                    {/* Handles both Arrays (Lists) and Strings */}
                    {analysis?.reasoning ? (
                        Array.isArray(analysis.reasoning) 
                            ? analysis.reasoning.map((r, i) => <div key={i}>• {r}</div>) 
                            : analysis.reasoning
                        ) : (analysis?.reason || "Scanning market structure...")
                    }
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