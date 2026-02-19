import React, { useState, useEffect, useRef } from 'react';
import { TrendingUp, Activity, RefreshCw, History, AlertTriangle, Clock, Lock, Target, Shield, Crosshair } from 'lucide-react';
import axios from 'axios';

// ☁️ LIVE SERVER ADDRESS (Ensure this matches your backend)
const API_URL = 'https://aura-trade-v1.onrender.com';

// 🧠 AI PERSONALITY SETTINGS
const ENTRY_THRESHOLD = 70;  // Slightly lower threshold for SMC (it's stricter)
const EXIT_THRESHOLD = 55;   
const FLIP_THRESHOLD = 80;   
const LOCK_EXPIRY_HOURS = 4; 

const SignalCard = ({ externalData, loading, onRefresh }) => {
  // --- STATE ---
  const [analysis, setAnalysis] = useState(null);
  const [newsLoading, setNewsLoading] = useState(false);
  
  // 📰 Real News State
  const [nextNews, setNextNews] = useState(null); 
  const [newsCountdown, setNewsCountdown] = useState('--:--:--');

  // 🔒 PERSISTENT SMART LOCK ENGINE
  const getSavedLock = () => {
    try {
      const saved = localStorage.getItem('aura_ai_lock');
      if (saved) {
        const parsed = JSON.parse(saved);
        const ageHours = (Date.now() - parsed.timestamp) / (1000 * 60 * 60);
        if (ageHours < LOCK_EXPIRY_HOURS) return parsed;
      }
    } catch (e) { console.error("Storage Read Error", e); }
    return { type: 'NEUTRAL', confidence: 0, timestamp: Date.now() };
  };

  const activeSignalRef = useRef(getSavedLock()); 
  const [displayState, setDisplayState] = useState(activeSignalRef.current);

  // --- 1. BRIDGE: Sync & Stabilize ---
  useEffect(() => {
    if (externalData) {
      processNewData(externalData);
    }
  }, [externalData]);

  // --- ⚙️ THE PERSISTENT LOGIC ENGINE ---
  const processNewData = (newData) => {
    // Handle the new Python response format
    const rawConf = Math.max(0, newData.confidence || 0);
    const rawSignal = (newData.signal || 'NEUTRAL').toUpperCase();
    
    let currentLock = activeSignalRef.current.type; 
    let nextState = 'NEUTRAL';

    // 1. NEUTRAL STATE
    if (currentLock === 'NEUTRAL') {
      if (rawConf >= ENTRY_THRESHOLD) {
        nextState = rawSignal.includes('BUY') ? 'BUY' : 'SELL';
      } else {
        nextState = 'NEUTRAL';
      }
    } 
    // 2. LOCKED STATE
    else {
      const isOpposite = (currentLock === 'BUY' && rawSignal.includes('SELL')) || 
                         (currentLock === 'SELL' && rawSignal.includes('BUY'));
      
      if (isOpposite && rawConf >= FLIP_THRESHOLD) {
        nextState = rawSignal.includes('BUY') ? 'BUY' : 'SELL'; 
      } 
      else if (rawConf < EXIT_THRESHOLD) {
        nextState = 'NEUTRAL'; 
      } 
      else {
        nextState = currentLock; 
      }
    }

    const newLockState = { 
      type: nextState, 
      confidence: rawConf, 
      timestamp: Date.now() 
    };
    
    activeSignalRef.current = newLockState;
    localStorage.setItem('aura_ai_lock', JSON.stringify(newLockState));
    
    setAnalysis(newData);
    setDisplayState(newLockState);
  };

  // --- 2. INTERNAL BRAIN (Fetch) ---
  const fetchLocalAnalysis = async (newsEvent = null) => {
    setNewsLoading(true);
    try {
      const payload = {
        timeframe: '1h',
        currency: 'USD',
        eventName: newsEvent ? newsEvent.event : null 
      };

      const res = await axios.post(`${API_URL}/api/analyze`, payload);
      if (res.data) {
          processNewData(res.data);
      }
    } catch (err) {
      console.error("❌ AI Analysis Failed:", err);
    } finally {
      setNewsLoading(false);
    }
  };

  // --- 3. NEWS FILTER ---
  const fetchDailyNews = async () => {
      try {
          const res = await axios.get(`${API_URL}/api/news`);
          const today = new Date().toDateString(); 
          const criticalEvent = res.data.find(n => {
              const newsDate = new Date(n.time * 1000).toDateString();
              const eventTime = new Date(n.time * 1000);
              const isFuture = eventTime > new Date();
              return (newsDate === today && n.impact === 'High' && n.currency === 'USD' && isFuture);
          });

          if (criticalEvent) {
            setNextNews(criticalEvent);
            fetchLocalAnalysis(criticalEvent); 
          } else {
            setNextNews(null);
          }
      } catch (err) { console.error("News Fetch Failed:", err); }
  };

  // --- 4. COUNTDOWN TIMER ---
  useEffect(() => {
    if (!nextNews) return;
    const eventTime = nextNews.time > 2000000000 ? nextNews.time : nextNews.time * 1000;
    const targetDate = new Date(eventTime);
    
    const timer = setInterval(() => {
      const diff = targetDate - new Date();
      if (diff <= 0) { setNextNews(null); setNewsCountdown(""); onRefresh(); return; }
      const h = Math.floor((diff / (1000 * 60 * 60)) % 24);
      const m = Math.floor((diff / (1000 * 60)) % 60);
      const s = Math.floor((diff / 1000) % 60);
      setNewsCountdown(`${h}h ${m}m ${s}s`);
    }, 1000);
    return () => clearInterval(timer);
  }, [nextNews, onRefresh]);

  useEffect(() => { fetchDailyNews(); setInterval(fetchDailyNews, 60000); }, []); 

  // --- UI HELPERS ---
  const getSignalColor = (type) => {
      if (type === 'BUY') return '#00E676'; 
      if (type === 'SELL') return '#FF1744'; 
      return '#FFC107'; 
  };

  const signalColor = getSignalColor(displayState.type);
  const isLocked = displayState.type !== 'NEUTRAL';
  
  const radius = 36;
  const circumference = 2 * Math.PI * radius;
  const strokeDashoffset = circumference - (displayState.confidence / 100) * circumference;
  const isBusy = loading || newsLoading;
  const cardBorder = nextNews ? '1px solid #ef5350' : `1px solid ${isLocked ? signalColor : 'rgba(255,255,255,0.08)'}`;

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
      boxShadow: isLocked ? `0 0 15px ${signalColor}20` : '0 4px 20px rgba(0,0,0,0.2)',
      transition: 'border 0.3s ease'
    }}>
      
      {/* HEADER */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ fontSize: '15px', margin: 0, color: '#9ca3af', display: 'flex', alignItems: 'center', gap: '8px', fontWeight: '600' }}>
             <Activity size={16} color={signalColor} /> AI BRAIN
        </h2>
        <div style={{display: 'flex', gap: '8px', alignItems: 'center'}}>
           {/* LOCK BADGE */}
           {isLocked && (
             <div style={{fontSize:'10px', background: `${signalColor}20`, color: signalColor, padding:'2px 8px', borderRadius:'4px', display:'flex', alignItems:'center', gap:'4px', fontWeight:'bold', border: `1px solid ${signalColor}40`}}>
               <Lock size={10} /> LOCKED
             </div>
           )}
           <button onClick={onRefresh} disabled={isBusy} style={{ background: 'rgba(255,255,255,0.05)', border: 'none', color: '#9ca3af', borderRadius: '6px', cursor: 'pointer', padding: '6px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <RefreshCw size={14} className={isBusy ? "spin" : ""} />
              <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }`}</style>
           </button>
        </div>
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
                        {displayState.confidence}%
                    </span>
                    <span style={{ fontSize: '10px', color: signalColor, opacity: 0.8, marginTop: '4px', fontWeight: 'bold', letterSpacing: '1px' }}>
                        {displayState.type}
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
                         {displayState.type === 'BUY' ? <TrendingUp size={18} /> : 
                          displayState.type === 'SELL' ? <TrendingUp size={18} style={{transform:'scaleY(-1)'}} /> : 
                          <Shield size={18} />}
                    </div>
                </div>
            </div>

            {/* 2. LOGIC / REASONING (🔥 ALWAYS VISIBLE NOW 🔥) */}
            <div style={{ background: 'rgba(255,255,255,0.02)', padding: '12px', borderRadius: '8px', borderLeft: `3px solid ${signalColor}` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px', color: signalColor, fontSize: '11px', fontWeight: 'bold' }}>
                    <History size={14} /> STRATEGY LOGIC
                </div>
                <div style={{ fontSize: '12px', color: '#d1d5db', lineHeight: '1.5', maxHeight: '100px', overflowY: 'auto', whiteSpace: 'pre-wrap', fontFamily: 'monospace' }}>
                    <div style={{display: 'flex', flexDirection: 'column', gap: '4px'}}>
                       {analysis?.reasoning && Array.isArray(analysis.reasoning) 
                           ? analysis.reasoning.map((r, i) => (
                               <div key={i} style={{display: 'flex', gap: '6px'}}>
                                  <span style={{color: signalColor}}>•</span>
                                  <span>{r.replace(/OB/g, 'Order Block')}</span>
                               </div>
                             )) 
                           : (analysis?.reason || "Scanning market structure...")
                       }
                    </div>
                </div>
            </div>

            {/* 3. NEW: TRADE SETUP CARD (SMC LEVELS) */}
            {isLocked && analysis?.tradeSetup && (
                <div style={{ background: 'rgba(0,0,0,0.3)', padding: '10px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                         <span style={{fontSize: '10px', color: '#9ca3af', display: 'flex', alignItems: 'center', gap: '4px'}}>
                            <Crosshair size={12}/> ENTRY
                         </span>
                         <span style={{fontSize: '12px', fontWeight: 'bold', color: '#fff'}}>{analysis.tradeSetup.entry}</span>
                    </div>
                    <div style={{ display: 'flex', gap: '8px' }}>
                        <div style={{ flex: 1, background: 'rgba(239, 83, 80, 0.15)', padding: '6px', borderRadius: '4px', textAlign: 'center' }}>
                            <div style={{ fontSize: '9px', color: '#ef5350', fontWeight: 'bold' }}>STOP LOSS</div>
                            <div style={{ fontSize: '11px', color: '#ffcdd2' }}>{analysis.tradeSetup.stop_loss}</div>
                        </div>
                        <div style={{ flex: 1, background: 'rgba(0, 230, 118, 0.15)', padding: '6px', borderRadius: '4px', textAlign: 'center' }}>
                            <div style={{ fontSize: '9px', color: '#00e676', fontWeight: 'bold' }}>TAKE PROFIT</div>
                            <div style={{ fontSize: '11px', color: '#b9f6ca' }}>{analysis.tradeSetup.take_profit}</div>
                        </div>
                    </div>
                    <div style={{textAlign: 'center', marginTop: '6px', fontSize: '9px', color: '#9ca3af'}}>
                        RR: 1:{analysis.tradeSetup.risk_reward}
                    </div>
                </div>
            )}

            {/* 4. IMPACT ALERT */}
            {nextNews && (
                <div style={{ background: 'rgba(239, 83, 80, 0.1)', padding: '12px', borderRadius: '8px', border: '1px solid rgba(239, 83, 80, 0.2)', animation: 'pulse 2s infinite' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#ef5350', fontSize: '11px', fontWeight: 'bold' }}><AlertTriangle size={14} /> NEWS ALERT</div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '4px', color: '#ef5350', fontSize: '12px', fontFamily: 'monospace', fontWeight: 'bold' }}><Clock size={12} /> {newsCountdown}</div>
                    </div>
                    <div style={{ fontSize: '11px', color: '#ff8a80', marginTop: '4px' }}>Upcoming: <b>{nextNews.title || nextNews.event || "High Impact News"}</b></div>
                    <style>{`@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(239, 83, 80, 0.4); } 70% { box-shadow: 0 0 0 10px rgba(239, 83, 80, 0); } 100% { box-shadow: 0 0 0 0 rgba(239, 83, 80, 0); } }`}</style>
                </div>
            )}

            {/* 5. QUIET STATE FOOTER */}
            {!isLocked && (
                <div style={{ marginTop: 'auto', padding: '15px', textAlign: 'center', border: '1px dashed #374357', borderRadius: '8px', color: '#fbbf24', fontSize: '12px', fontStyle: 'italic', backgroundColor: 'rgba(251, 191, 36, 0.05)' }}>
                    Awaiting high-probability setup (&gt;{ENTRY_THRESHOLD}%)
                </div>
            )}
        </>
      )}
    </div>
  );
};

export default SignalCard;