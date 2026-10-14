import React, { useState, useEffect, useRef } from 'react';
import { TrendingUp, Activity, RefreshCw, History, AlertTriangle, Clock, Lock, Shield, Crosshair, Calculator, Layers } from 'lucide-react';
import axios from 'axios';

const API_URL = 'https://aura-trade-v1.onrender.com';

const ENTRY_THRESHOLD = 65;
const EXIT_THRESHOLD  = 50;
const FLIP_THRESHOLD  = 72;

const SESSIONS = [
  { name: 'TOKYO',    start: 0,  end: 9,  color: '#7c3aed' },
  { name: 'LONDON',   start: 8,  end: 17, color: '#0ea5e9' },
  { name: 'NEW YORK', start: 13, end: 22, color: '#f59e0b' },
  { name: 'SYDNEY',   start: 21, end: 6,  color: '#10b981' },
];

const getActiveSession = () => {
  const h = new Date().getUTCHours();
  return SESSIONS.find(s => {
    if (s.start < s.end) return h >= s.start && h < s.end;
    return h >= s.start || h < s.end;
  }) || null;
};

const calcLotSize = (balance, riskPct, entryPrice, slPrice, pipValue = 10) => {
  if (!balance || !entryPrice || !slPrice) return 0;
  const riskAmount = (balance * riskPct) / 100;
  const priceDiff  = Math.abs(entryPrice - slPrice);
  if (priceDiff === 0) return 0;

  let slPips;
  if (entryPrice > 500) {
    slPips = priceDiff * 10;
  } else if (entryPrice > 20) {
    slPips = priceDiff * 100;
  } else {
    slPips = priceDiff * 10000;
  }

  if (slPips === 0) return 0;
  const lots = riskAmount / (slPips * pipValue);
  return Math.max(0.01, parseFloat(lots.toFixed(2)));
};

const SignalCard = ({ externalData, loading, onRefresh }) => {
  const [analysis,      setAnalysis]      = useState(null);
  const [upcomingNews,  setUpcomingNews]  = useState([]);
  const [now,           setNow]           = useState(Date.now());
  const [activeTab,     setActiveTab]     = useState('signal');
  const [activeSession, setActiveSession] = useState(getActiveSession());

  const [balance,  setBalance]  = useState(10000);
  const [riskPct,  setRiskPct]  = useState(1);
  const [pipValue, setPipValue] = useState(10);

  const locksByPairRef = useRef({});
  const [displayState, setDisplayState] = useState({
    type: 'WAIT',
    bias: 'NEUTRAL',
    confidence: 0,
    isLocked: false,
    timestamp: Date.now()
  });

  useEffect(() => {
    if (externalData) processNewData(externalData);
  }, [externalData]);

  const processNewData = (newData) => {
    const rawConf   = Math.max(0, newData.confidence || 0);
    const rawSignal = (newData.signal || 'WAIT').toUpperCase();
    const rawTrend  = (newData.trend  || '').toUpperCase();
    const pairKey   = `${newData.trend || 'PAIR'}_${newData?.keyLevels?.ema200 || ''}`;

    const cycleBias = rawTrend.includes('BULLISH') ? 'BULLISH'
                    : rawTrend.includes('BEARISH') ? 'BEARISH'
                    : 'NEUTRAL';

    const hasValidTradeSetup = Boolean(newData.tradeSetup && newData.tradeSetup.entry);
    const prevLock = locksByPairRef.current[pairKey] || { type: 'WAIT', confidence: 0, isLocked: false };

    let nextType = 'WAIT';
    let locked   = false;

    if ((rawSignal === 'BUY' || rawSignal === 'SELL') && hasValidTradeSetup && rawConf >= ENTRY_THRESHOLD) {
      nextType = rawSignal;
      locked   = true;
    } else if (prevLock.isLocked && hasValidTradeSetup) {
      const isOpposite = (prevLock.type === 'BUY' && rawSignal === 'SELL') ||
                         (prevLock.type === 'SELL' && rawSignal === 'BUY');
      if (isOpposite && rawConf >= FLIP_THRESHOLD) {
        nextType = rawSignal;
        locked   = true;
      } else if (rawConf >= EXIT_THRESHOLD) {
        nextType = prevLock.type;
        locked   = true;
      }
    }

    const newState = {
      type:       nextType,
      bias:       cycleBias,
      confidence: rawConf,
      isLocked:   locked,
      timestamp:  Date.now(),
    };

    locksByPairRef.current[pairKey] = newState;
    setAnalysis(newData);
    setDisplayState(newState);
  };

  const fetchDailyNews = async () => {
    try {
      const res   = await axios.get(`${API_URL}/api/news`);
      const today = new Date().toDateString();
      const events = (res.data || []).filter(n => {
        const ts        = n.time > 2000000000 ? n.time : n.time * 1000;
        const newsDate  = new Date(ts).toDateString();
        const eventTime = new Date(ts);
        return newsDate === today && n.impact === 'High' && n.currency === 'USD' && eventTime > new Date();
      }).sort((a, b) => a.time - b.time);
      setUpcomingNews(events);
    } catch { /* silent */ }
  };

  useEffect(() => {
    fetchDailyNews();
    const newsId = setInterval(fetchDailyNews, 60000);
    const tickId = setInterval(() => {
      setNow(Date.now());
      setActiveSession(getActiveSession());
    }, 1000);
    return () => { clearInterval(newsId); clearInterval(tickId); };
  }, []);

  const getCountdown = (target) => {
    const diff = target - now;
    if (diff <= 0) return 'Releasing Now...';
    const h   = Math.floor(diff / 3600000);
    const m   = Math.floor((diff % 3600000) / 60000);
    const sec = Math.floor((diff % 60000) / 1000);
    return `${h}h ${String(m).padStart(2, '0')}m ${String(sec).padStart(2, '0')}s`;
  };

  const signalColor = displayState.type === 'BUY'  ? '#00e676'
                    : displayState.type === 'SELL' ? '#ff4757'
                    : displayState.bias === 'BULLISH' ? '#00e676'
                    : displayState.bias === 'BEARISH' ? '#ff4757'
                    : '#f0b429';

  const statusLabel = displayState.type === 'BUY'  ? 'BUY ACTIVE'
                    : displayState.type === 'SELL' ? 'SELL ACTIVE'
                    : displayState.bias === 'BULLISH' ? 'WAIT FOR BUY'
                    : displayState.bias === 'BEARISH' ? 'WAIT FOR SELL'
                    : 'WAIT / NEUTRAL';

  const isLocked = displayState.isLocked;
  const isBusy   = loading;

  const circumference = 2 * Math.PI * 32;
  const dashOffset    = circumference - (displayState.confidence / 100) * circumference;

  const lotSize = calcLotSize(
    balance,
    riskPct,
    parseFloat(analysis?.tradeSetup?.entry) || 0,
    parseFloat(analysis?.tradeSetup?.stop_loss) || 0,
    pipValue
  );
  const riskAmount = ((balance * riskPct) / 100).toFixed(2);

  const mtfData  = analysis?.mtf_confluence || [];
  const poiLevel = analysis?.keyLevels?.poi;
  const target1  = analysis?.keyLevels?.target1 || analysis?.tradeSetup?.target_1;
  const target2  = analysis?.keyLevels?.target2 || analysis?.tradeSetup?.target_2;

  const tabBtnStyle = (id) => ({
    flex: 1, padding: '7px 4px', background: activeTab === id ? 'rgba(240,180,41,0.1)' : 'transparent',
    border: `1px solid ${activeTab === id ? 'rgba(240,180,41,0.3)' : 'rgba(255,255,255,0.06)'}`,
    color: activeTab === id ? '#f0b429' : '#6b7a8d', cursor: 'pointer',
    borderRadius: 4, fontSize: 10, fontFamily: "'Space Mono', monospace",
    fontWeight: 700, letterSpacing: 0.5, transition: 'all 0.15s',
  });

  return (
    <div style={{
      height: '100%', background: 'rgba(13,17,23,0.95)',
      border: upcomingNews.length > 0
        ? '1px solid rgba(239,83,80,0.5)'
        : `1px solid ${isLocked ? signalColor + '55' : 'rgba(255,255,255,0.08)'}`,
      borderRadius: 12, padding: 16, color: '#e8edf3',
      display: 'flex', flexDirection: 'column', gap: 10,
      transition: 'border 0.3s ease', overflow: 'hidden',
      fontFamily: "'Syne', sans-serif",
    }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Activity size={14} color={signalColor} />
          <span style={{ fontSize: 12, color: '#9ca3af', fontWeight: 600, letterSpacing: 1, fontFamily: "'Space Mono', monospace" }}>AI BRAIN</span>
          {activeSession && (
            <span style={{ fontSize: 9, background: `${activeSession.color}18`, color: activeSession.color, border: `1px solid ${activeSession.color}40`, padding: '2px 6px', borderRadius: 3, fontFamily: "'Space Mono', monospace", fontWeight: 700, letterSpacing: 1 }}>
              {activeSession.name.split(' ')[0]}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          {isLocked ? (
            <div style={{ fontSize: 9, background: `${signalColor}18`, color: signalColor, border: `1px solid ${signalColor}40`, padding: '2px 8px', borderRadius: 3, display: 'flex', alignItems: 'center', gap: 3, fontWeight: 700, fontFamily: "'Space Mono', monospace" }}>
              <Lock size={9} /> SIGNAL ACTIVE
            </div>
          ) : (
            <div style={{ fontSize: 9, background: 'rgba(240,180,41,0.1)', color: '#f0b429', border: '1px solid rgba(240,180,41,0.3)', padding: '2px 8px', borderRadius: 3, fontWeight: 700, fontFamily: "'Space Mono', monospace" }}>
              WAITING FOR ZONE
            </div>
          )}
          <button onClick={onRefresh} disabled={isBusy} style={{ background: 'rgba(255,255,255,0.05)', border: 'none', color: '#9ca3af', borderRadius: 5, cursor: 'pointer', padding: 5, display: 'flex', alignItems: 'center' }}>
            <RefreshCw size={13} style={isBusy ? { animation: 'spin 1s linear infinite' } : {}} />
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
        <button style={tabBtnStyle('signal')} onClick={() => setActiveTab('signal')}><Activity size={10} style={{ display: 'inline', marginRight: 4 }} />SIGNAL</button>
        <button style={tabBtnStyle('risk')}   onClick={() => setActiveTab('risk')}><Calculator size={10} style={{ display: 'inline', marginRight: 4 }} />RISK</button>
        <button style={tabBtnStyle('mtf')}    onClick={() => setActiveTab('mtf')}><Layers size={10} style={{ display: 'inline', marginRight: 4 }} />MTF</button>
      </div>

      {isBusy && !analysis ? (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: 8, color: '#6b7a8d' }}>
          <RefreshCw size={18} style={{ animation: 'spin 1s linear infinite' }} />
          <span style={{ fontSize: 11, fontFamily: "'Space Mono', monospace" }}>Simulating outcomes...</span>
        </div>
      ) : (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 10, overflowY: 'auto', minHeight: 0, paddingRight: 2 }}>

          {/* ─── TAB: SIGNAL ─── */}
          {activeTab === 'signal' && (
            <>
              {/* Readiness ring + Strategy Name */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, background: 'rgba(255,255,255,0.02)', padding: 12, borderRadius: 8, border: '1px solid rgba(255,255,255,0.04)', flexShrink: 0 }}>
                <div style={{ position: 'relative', width: 72, height: 72, flexShrink: 0 }}>
                  <svg width="72" height="72" style={{ transform: 'rotate(-90deg)' }}>
                    <circle cx="36" cy="36" r="30" stroke="rgba(255,255,255,0.06)" strokeWidth="6" fill="transparent" />
                    <circle cx="36" cy="36" r="30" stroke={signalColor} strokeWidth="6" fill="transparent"
                      strokeDasharray={circumference} strokeDashoffset={dashOffset} strokeLinecap="round"
                      style={{ transition: 'stroke-dashoffset 0.8s ease-in-out' }} />
                  </svg>
                  <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    {displayState.bias === 'BULLISH' ? <TrendingUp size={16} color={signalColor} /> :
                     displayState.bias === 'BEARISH' ? <TrendingUp size={16} color={signalColor} style={{ transform: 'scaleY(-1)' }} /> :
                     <Shield size={16} color={signalColor} />}
                  </div>
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 8, color: '#f0b429', fontFamily: "'Space Mono', monospace", letterSpacing: 1, marginBottom: 2, fontWeight: 700 }}>
                    STRATEGY: MMM PEAK & ZONE SNIPER
                  </div>
                  <div style={{ fontSize: 24, fontWeight: 800, color: signalColor, lineHeight: 1 }}>{displayState.confidence}%</div>
                  <div style={{ fontSize: 11, color: signalColor, fontWeight: 700, letterSpacing: 1.5, fontFamily: "'Space Mono', monospace", marginTop: 4 }}>
                    {statusLabel}
                  </div>
                </div>
              </div>

              {/* Planned Entry Zone + Targets Roadmap (Shown while waiting for price to tap the zone) */}
              {(target1 || target2) && !analysis?.tradeSetup && (
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 5, flexShrink: 0 }}>
                  <div style={{ background: 'rgba(41,98,255,0.10)', padding: '6px 4px', borderRadius: 5, textAlign: 'center', border: '1px solid rgba(41,98,255,0.35)' }}>
                    <div style={{ fontSize: 8, color: '#82b1ff', fontWeight: 700, fontFamily: "'Space Mono', monospace", marginBottom: 2 }}>
                      {displayState.bias === 'BULLISH' ? 'BUY ZONE 🎯' : 'SELL ZONE 🎯'}
                    </div>
                    <div style={{ fontSize: 11, fontWeight: 700, color: '#fff', fontFamily: "'Space Mono', monospace" }}>{poiLevel ?? '—'}</div>
                  </div>
                  <div style={{ background: 'rgba(240,180,41,0.07)', padding: '6px 4px', borderRadius: 5, textAlign: 'center', border: '1px solid rgba(240,180,41,0.25)' }}>
                    <div style={{ fontSize: 8, color: '#f0b429', fontWeight: 700, fontFamily: "'Space Mono', monospace", marginBottom: 2 }}>TARGET 1</div>
                    <div style={{ fontSize: 11, fontWeight: 700, color: '#ffe082', fontFamily: "'Space Mono', monospace" }}>{target1 ?? '—'}</div>
                  </div>
                  <div style={{ background: 'rgba(240,180,41,0.07)', padding: '6px 4px', borderRadius: 5, textAlign: 'center', border: '1px solid rgba(240,180,41,0.25)' }}>
                    <div style={{ fontSize: 8, color: '#f0b429', fontWeight: 700, fontFamily: "'Space Mono', monospace", marginBottom: 2 }}>TARGET 2 🚀</div>
                    <div style={{ fontSize: 11, fontWeight: 700, color: '#ffe082', fontFamily: "'Space Mono', monospace" }}>{target2 ?? '—'}</div>
                  </div>
                </div>
              )}

              {/* Active Trade Setup Levels (Shown when BUY or SELL triggers) */}
              {analysis?.tradeSetup && (
                <div style={{ background: 'rgba(0,0,0,0.32)', padding: 10, borderRadius: 7, border: `1px solid ${signalColor}40`, flexShrink: 0 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                    <span style={{ fontSize: 9, color: '#9ca3af', display: 'flex', alignItems: 'center', gap: 4, fontFamily: "'Space Mono', monospace" }}>
                      <Crosshair size={11} color="#2962FF" /> LIVE ENTRY
                    </span>
                    <span style={{ fontSize: 12, fontWeight: 700, color: '#fff', fontFamily: "'Space Mono', monospace" }}>{analysis.tradeSetup.entry}</span>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 5 }}>
                    <div style={{ background: 'rgba(255,71,87,0.12)', padding: '6px 4px', borderRadius: 4, textAlign: 'center', border: '1px solid rgba(255,71,87,0.25)' }}>
                      <div style={{ fontSize: 8, color: '#ff4757', fontWeight: 700, fontFamily: "'Space Mono', monospace", marginBottom: 2 }}>STOP LOSS</div>
                      <div style={{ fontSize: 10, color: '#ffcdd2', fontFamily: "'Space Mono', monospace" }}>{analysis.tradeSetup.stop_loss}</div>
                    </div>
                    <div style={{ background: 'rgba(0,230,118,0.12)', padding: '6px 4px', borderRadius: 4, textAlign: 'center', border: '1px solid rgba(0,230,118,0.25)' }}>
                      <div style={{ fontSize: 8, color: '#00e676', fontWeight: 700, fontFamily: "'Space Mono', monospace", marginBottom: 2 }}>TARGET 1</div>
                      <div style={{ fontSize: 10, color: '#b9f6ca', fontFamily: "'Space Mono', monospace" }}>{analysis.tradeSetup.take_profit}</div>
                    </div>
                    <div style={{ background: 'rgba(0,230,118,0.18)', padding: '6px 4px', borderRadius: 4, textAlign: 'center', border: '1px solid rgba(0,230,118,0.35)' }}>
                      <div style={{ fontSize: 8, color: '#00e676', fontWeight: 700, fontFamily: "'Space Mono', monospace", marginBottom: 2 }}>TARGET 2</div>
                      <div style={{ fontSize: 10, color: '#b9f6ca', fontFamily: "'Space Mono', monospace" }}>{analysis.tradeSetup.target_2 || analysis.tradeSetup.take_profit}</div>
                    </div>
                  </div>
                  <div style={{ textAlign: 'center', marginTop: 6, fontSize: 10, color: '#00e676', fontWeight: 700, fontFamily: "'Space Mono', monospace" }}>
                    REWARD / RISK 1:{analysis.tradeSetup.risk_reward}
                  </div>
                </div>
              )}

              {/* 4-Step Strategy Checklist */}
              <div style={{ background: 'rgba(255,255,255,0.02)', padding: '10px 12px', borderRadius: 7, borderLeft: `3px solid ${signalColor}`, flex: 1, minHeight: 120, display: 'flex', flexDirection: 'column' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 8, color: signalColor, fontSize: 10, fontWeight: 700, fontFamily: "'Space Mono', monospace", flexShrink: 0 }}>
                  <History size={12} /> 4-STEP ENTRY CHECKLIST
                </div>
                <div style={{ fontSize: 11, color: '#d1d5db', lineHeight: 1.55, overflowY: 'auto', paddingTop: 2, paddingRight: 4 }}>
                  {analysis?.reasoning && Array.isArray(analysis.reasoning)
                    ? analysis.reasoning.map((r, i) => (
                        <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 6, marginBottom: 6 }}>
                          <span style={{ flex: 1 }}>{r}</span>
                        </div>
                      ))
                    : (analysis?.reason || 'Scanning market structure...')}
                </div>
              </div>

              {upcomingNews.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, flexShrink: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 5, color: '#ef5350', fontSize: 10, fontWeight: 700, fontFamily: "'Space Mono', monospace" }}>
                    <AlertTriangle size={12} /> HIGH-IMPACT EVENTS
                  </div>
                  {upcomingNews.map((news, i) => (
                    <div key={i} style={{ background: 'rgba(239,83,80,0.08)', padding: '8px 10px', borderRadius: 6, border: '1px solid rgba(239,83,80,0.2)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: 10, color: '#ff8a80', fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '55%' }}>{news.title || news.event}</span>
                      <span style={{ fontSize: 10, color: '#ef5350', fontFamily: "'Space Mono', monospace", display: 'flex', alignItems: 'center', gap: 3 }}>
                        <Clock size={10} /> {getCountdown(news.time > 2000000000 ? news.time : news.time * 1000)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {/* ─── TAB: RISK CALCULATOR ─── */}
          {activeTab === 'risk' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ fontSize: 10, color: '#6b7a8d', fontFamily: "'Space Mono', monospace", letterSpacing: 1 }}>POSITION SIZING</div>

              <RiskInput label="Account Balance ($)" value={balance} onChange={setBalance} min={100} max={1000000} step={100} />
              <RiskInput label="Risk %" value={riskPct} onChange={setRiskPct} min={0.1} max={10} step={0.1} decimals={1} />
              <RiskInput label="Pip Value ($)" value={pipValue} onChange={setPipValue} min={1} max={100} step={1} />

              <div style={{ height: 1, background: 'rgba(255,255,255,0.05)', margin: '4px 0' }} />

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                <ResultBox label="LOT SIZE"    value={lotSize || '—'} color="#f0b429" unit={lotSize ? 'lots' : 'awaiting entry'} />
                <ResultBox label="RISK AMOUNT" value={`$${riskAmount}`} color="#ff4757" />
              </div>

              {analysis?.tradeSetup && (
                <div style={{ background: 'rgba(255,255,255,0.02)', padding: 10, borderRadius: 6, border: '1px solid rgba(255,255,255,0.05)', fontSize: 11, fontFamily: "'Space Mono', monospace" }}>
                  <div style={{ color: '#6b7a8d', marginBottom: 6, fontSize: 9, letterSpacing: 1 }}>ACTIVE TRADE SETUP</div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                    <span style={{ color: '#9ca3af' }}>Entry</span>
                    <span style={{ color: '#fff' }}>{analysis.tradeSetup.entry}</span>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                    <span style={{ color: '#9ca3af' }}>Stop Loss</span>
                    <span style={{ color: '#ff4757' }}>{analysis.tradeSetup.stop_loss}</span>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                    <span style={{ color: '#9ca3af' }}>Target 1 / Target 2</span>
                    <span style={{ color: '#00e676' }}>{analysis.tradeSetup.take_profit} / {analysis.tradeSetup.target_2 || '—'}</span>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <span style={{ color: '#9ca3af' }}>RR</span>
                    <span style={{ color: '#00e676' }}>1:{analysis.tradeSetup.risk_reward}</span>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* ─── TAB: MTF CONFLUENCE ─── */}
          {activeTab === 'mtf' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ fontSize: 10, color: '#6b7a8d', fontFamily: "'Space Mono', monospace", letterSpacing: 1 }}>MULTI-TIMEFRAME ALIGNMENT</div>
              {mtfData.map((tf, i) => {
                const biasStr = (tf.bias || 'NEUTRAL').toString().toUpperCase();
                const tfColor = (biasStr.includes('BULL') || biasStr.includes('BUY'))  ? '#00e676'
                              : (biasStr.includes('BEAR') || biasStr.includes('SELL')) ? '#ff4757'
                              : '#f0b429';
                return (
                  <div key={i} style={{ background: 'rgba(255,255,255,0.02)', padding: '10px 12px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.05)' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                      <span style={{ fontSize: 12, fontWeight: 700, fontFamily: "'Space Mono', monospace", color: '#e8edf3' }}>{tf.tf}</span>
                      <span style={{ fontSize: 10, fontWeight: 700, color: tfColor, fontFamily: "'Space Mono', monospace", background: `${tfColor}15`, padding: '2px 8px', borderRadius: 3 }}>
                        {biasStr.replace('STRONG_', '')}
                      </span>
                    </div>
                    <div style={{ height: 4, background: 'rgba(255,255,255,0.05)', borderRadius: 2, overflow: 'hidden' }}>
                      <div style={{ height: '100%', width: `${tf.strength || 0}%`, background: tfColor, borderRadius: 2, transition: 'width 0.5s ease' }} />
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 3 }}>
                      <span style={{ fontSize: 9, color: '#3d4d5e', fontFamily: "'Space Mono', monospace" }}>STRENGTH</span>
                      <span style={{ fontSize: 9, color: tfColor, fontFamily: "'Space Mono', monospace" }}>{tf.strength || 0}%</span>
                    </div>
                  </div>
                );
              })}
              <div style={{ marginTop: 4, padding: '8px 10px', background: 'rgba(255,255,255,0.02)', borderRadius: 6, border: '1px solid rgba(255,255,255,0.04)' }}>
                <div style={{ fontSize: 9, color: '#6b7a8d', fontFamily: "'Space Mono', monospace", letterSpacing: 1, marginBottom: 4 }}>CONFLUENCE SCORE</div>
                <div style={{ fontSize: 20, fontWeight: 800, color: signalColor, fontFamily: "'Space Mono', monospace" }}>
                  {mtfData.length > 0 ? Math.round(mtfData.filter(t => {
                    const b = (t.bias || '').toString().toUpperCase();
                    return b.includes('BULL') || b.includes('BEAR');
                  }).length / mtfData.length * 100) : 0}%
                </div>
                <div style={{ fontSize: 10, color: '#6b7a8d', marginTop: 2 }}>timeframes aligned with cycle</div>
              </div>
            </div>
          )}
        </div>
      )}

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;700;800&display=swap');
        .spin { animation: spin 1s linear infinite; }
        @keyframes spin { 0%{transform:rotate(0deg)} 100%{transform:rotate(360deg)} }
        ::-webkit-scrollbar { width: 3px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #1e2a38; border-radius: 2px; }
      `}</style>
    </div>
  );
};

const RiskInput = ({ label, value, onChange, min, max, step, decimals = 0 }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
      <span style={{ fontSize: 10, color: '#6b7a8d', fontFamily: "'Space Mono', monospace" }}>{label}</span>
      <span style={{ fontSize: 10, color: '#e8edf3', fontFamily: "'Space Mono', monospace", fontWeight: 700 }}>
        {decimals > 0 ? parseFloat(value).toFixed(decimals) : value}
      </span>
    </div>
    <input
      type="range" min={min} max={max} step={step} value={value}
      onChange={e => onChange(parseFloat(e.target.value))}
      style={{ width: '100%', accentColor: '#f0b429', cursor: 'pointer' }}
    />
  </div>
);

const ResultBox = ({ label, value, color, unit }) => (
  <div style={{ background: 'rgba(0,0,0,0.3)', padding: '10px 12px', borderRadius: 6, border: `1px solid ${color}25`, textAlign: 'center' }}>
    <div style={{ fontSize: 9, color: '#6b7a8d', fontFamily: "'Space Mono', monospace", letterSpacing: 1, marginBottom: 4 }}>{label}</div>
    <div style={{ fontSize: 18, fontWeight: 800, color, fontFamily: "'Space Mono', monospace", lineHeight: 1 }}>{value}</div>
    {unit && <div style={{ fontSize: 9, color, opacity: 0.6, marginTop: 2, fontFamily: "'Space Mono', monospace" }}>{unit}</div>}
  </div>
);

export default SignalCard;