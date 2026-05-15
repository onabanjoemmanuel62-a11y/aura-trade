import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import ChartComponent from './ChartComponent';
import SignalCard from './SignalCard';
import HistoryTable from './HistoryTable';
// import './App.css'; // Make sure this is still imported if you have external CSS

const API_URL = 'https://aura-trade-v1.onrender.com';

const ASSETS = [
  { id: 'GC=F',     name: 'XAU/USD',  label: 'Gold',        icon: '⬡', category: 'metals'  },
  { id: 'EURUSD=X',  name: 'EUR/USD',  label: 'Euro',        icon: '€', category: 'majors'  },
  { id: 'GBPUSD=X',  name: 'GBP/USD',  label: 'Cable',       icon: '£', category: 'majors'  },
  { id: 'JPY=X',     name: 'USD/JPY',  label: 'Yen',         icon: '¥', category: 'majors'  },
  { id: 'CHF=X',     name: 'USD/CHF',  label: 'Swissy',      icon: 'F', category: 'majors'  },
  { id: 'AUDUSD=X',  name: 'AUD/USD',  label: 'Aussie',      icon: 'A', category: 'minors'  },
  { id: 'CAD=X',     name: 'USD/CAD',  label: 'Loonie',      icon: 'C', category: 'minors'  },
  { id: 'NZDUSD=X',  name: 'NZD/USD',  label: 'Kiwi',        icon: 'N', category: 'minors'  },
];

const SESSIONS = [
  { name: 'TOKYO',    start: 0,  end: 9,  color: '#7c3aed', abbr: 'TKY' },
  { name: 'LONDON',   start: 8,  end: 17, color: '#0ea5e9', abbr: 'LON' },
  { name: 'NEW YORK', start: 13, end: 22, color: '#f59e0b', abbr: 'NYC' },
  { name: 'SYDNEY',   start: 21, end: 6,  color: '#10b981', abbr: 'SYD' },
];

const getActiveSessions = (utcHour) => {
  return SESSIONS.filter(s => {
    if (s.start < s.end) return utcHour >= s.start && utcHour < s.end;
    return utcHour >= s.start || utcHour < s.end; 
  });
};

const NAV_ITEMS = [
  { id: 'chart',    label: 'Chart',    icon: 'M3 3h18v2H3zM3 7h12v2H3zM3 11h18v2H3z' },
  { id: 'signals',  label: 'Signals',  icon: 'M13 2L3 14h9l-1 8 10-12h-9l1-8z' },
  { id: 'journal',  label: 'Journal',  icon: 'M4 6h16M4 10h16M4 14h10' },
  { id: 'settings', label: 'Settings', icon: 'M12 4a8 8 0 100 16A8 8 0 0012 4zm0 2a6 6 0 110 12A6 6 0 0112 6z' },
];

function App() {
  const [aiData,          setAiData]         = useState(null);
  const [loading,         setLoading]        = useState(false);
  const [activeSymbol,    setActiveSymbol]   = useState('GC=F');
  const [activeNav,       setActiveNav]      = useState('chart');
  const [utcTime,         setUtcTime]        = useState(new Date());
  const [activeSessions,  setActiveSessions] = useState([]);
  const [stats,           setStats]          = useState({ winRate: 0, totalPips: 0, signals: 0, streak: 0 });
  const candles5mRef = React.useRef([]);
  
  // 1. Fixed States for responsiveness and sidebar toggling
  const [isMobile, setIsMobile] = useState(window.innerWidth < 768);
  const [sidebarOpen, setSidebarOpen] = useState(window.innerWidth >= 768);

  // Handle window resize dynamically
  useEffect(() => {
    const handleResize = () => {
      const mobile = window.innerWidth < 768;
      setIsMobile(mobile);
      if (!mobile) setSidebarOpen(true); // Auto-open on desktop
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // UTC clock + session detection
  useEffect(() => {
    const tick = () => {
      const now = new Date();
      setUtcTime(now);
      setActiveSessions(getActiveSessions(now.getUTCHours()));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  // Fetch performance stats
  useEffect(() => {
    const fetchStats = async () => {
      try {
        const res = await axios.get(`${API_URL}/api/trades`);
        if (Array.isArray(res.data) && res.data.length > 0) {
          const trades = res.data;
          const wins = trades.filter(t => t.result === 'WON').length;
          const winRate = Math.round((wins / trades.length) * 100);
          const totalPips = trades.reduce((sum, t) => sum + (parseFloat(t.profit) || 0), 0);

          let streak = 0;
          for (let i = trades.length - 1; i >= 0; i--) {
            if (i === trades.length - 1) {
              streak = trades[i].result === 'WON' ? 1 : -1;
            } else {
              if (trades[i].result === 'WON' && streak > 0) streak++;
              else if (trades[i].result !== 'WON' && streak < 0) streak--;
              else break;
            }
          }
          setStats({ winRate, totalPips: totalPips.toFixed(1), signals: trades.length, streak });
        }
      } catch { /* silent */ }
    };
    fetchStats();
  }, []);

  const runAnalysis = useCallback(async (sym) => {
    setLoading(true);
    const symbol = sym || activeSymbol;
    try {
      // Fetch H1 candles
      const candleRes = await axios.get(`${API_URL}/api/candles/1h`, {
        params: { symbol, limit: 300, timestamp: Date.now() }
      });

      const htfRes = await axios.get(`${API_URL}/api/candles/4h`, {
        params: { symbol, limit: 100, timestamp: Date.now() }
      });

      const res5m = await axios.get(`${API_URL}/api/candles/5m`, {
        params: { symbol, limit: 250, timestamp: Date.now() }
      });
      candles5mRef.current = res5m.data || [];

      // Run analysis with all candle data
      const res = await axios.post(`${API_URL}/api/analyze`, {
        currency:    symbol,
        timeframe:   '1h',
        candles:     candles1h,
        htf_candles: candles4h,
        candles_5m:  candles5mRef.current,
      });

      if (res.data) setAiData(res.data);
    } catch (err) {
      console.error('Analysis error:', err);
    } finally {
      setLoading(false);
    }
  }, [activeSymbol]);

  useEffect(() => {
    runAnalysis(activeSymbol);
    const id = setInterval(() => runAnalysis(activeSymbol), 60000);
    return () => clearInterval(id);
  }, [activeSymbol, runAnalysis]);

  const pad = n => String(n).padStart(2, '0');
  const utcStr = `${pad(utcTime.getUTCHours())}:${pad(utcTime.getUTCMinutes())}:${pad(utcTime.getUTCSeconds())} UTC`;

  // Merge dynamic styles
  const s = getStyles(isMobile, sidebarOpen);

  return (
    <div style={s.root}>

      {/* ── MOBILE OVERLAY ── */}
      {isMobile && sidebarOpen && (
        <div style={s.mobileOverlay} onClick={() => setSidebarOpen(false)} />
      )}

      {/* ── SIDEBAR ── */}
      <aside style={s.sidebar}>
        <div style={s.logoWrap} onClick={() => setSidebarOpen(!sidebarOpen)}>
          <div style={s.logoMark}>A</div>
          {sidebarOpen && <span style={s.logoText}>AURA<span style={{ color: '#f0b429', fontWeight: 400 }}>TRADE</span></span>}
        </div>

        <nav style={s.nav}>
          {NAV_ITEMS.map(item => (
            <button
              key={item.id}
              onClick={() => {
                setActiveNav(item.id);
                if (isMobile) setSidebarOpen(false); // Close drawer on mobile after selection
              }}
              style={{ ...s.navBtn, ...(activeNav === item.id ? s.navBtnActive : {}) }}
              title={item.label}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                stroke={activeNav === item.id ? '#f0b429' : '#6b7a8d'}
                strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d={item.icon} />
              </svg>
              {sidebarOpen && <span style={{ ...s.navLabel, color: activeNav === item.id ? '#f0b429' : '#6b7a8d' }}>{item.label}</span>}
            </button>
          ))}
        </nav>

        <div style={s.sessionBox}>
          {sidebarOpen && <div style={s.sessionTitle}>KILLZONES</div>}
          {SESSIONS.map(ses => {
            const isActive = activeSessions.some(a => a.name === ses.name);
            return (
              <div key={ses.name} style={{ ...s.sessionRow, opacity: isActive ? 1 : 0.35 }} title={ses.name}>
                <div style={{ ...s.sessionDot, background: ses.color, boxShadow: isActive ? `0 0 6px ${ses.color}` : 'none' }} />
                {sidebarOpen && (
                  <div style={s.sessionInfo}>
                    <span style={{ fontSize: 11, color: isActive ? '#e8edf3' : '#6b7a8d', fontWeight: isActive ? 700 : 400 }}>{ses.name}</span>
                    {isActive && <span style={{ fontSize: 9, color: ses.color, fontWeight: 700, letterSpacing: 1 }}>ACTIVE</span>}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <div style={s.clockBox}>
          <div style={{ ...s.clockDot, animation: 'pulse 2s infinite' }} />
          {sidebarOpen && <span style={s.clockText}>{utcStr}</span>}
        </div>
      </aside>

      {/* ── MAIN ── */}
      <div style={s.main}>

        {/* ── TOPBAR ── */}
        <header style={s.topbar}>
          {/* Mobile Hamburger Menu */}
          {isMobile && (
             <button onClick={() => setSidebarOpen(true)} style={s.hamburgerBtn}>
               <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#e8edf3" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                 <line x1="3" y1="12" x2="21" y2="12"></line>
                 <line x1="3" y1="6" x2="21" y2="6"></line>
                 <line x1="3" y1="18" x2="21" y2="18"></line>
               </svg>
             </button>
          )}

          {/* Pair tabs - Made scrollable on mobile */}
          <div style={s.pairTabs}>
            {ASSETS.map(asset => (
              <button
                key={asset.id}
                onClick={() => setActiveSymbol(asset.id)}
                style={{ ...s.pairTab, ...(activeSymbol === asset.id ? s.pairTabActive : {}) }}
              >
                <span style={s.pairTabIcon}>{asset.icon}</span>
                <span>{asset.name}</span>
              </button>
            ))}
          </div>
          
          {/* Only show full stats on desktop to save space, or make them scrollable too */}
        {!isMobile && (
  <div style={s.statsBar}>
    <StatPill label="WIN RATE"   value={`${stats.winRate}%`}   color="#00e676" />
    <StatPill label="PIPS"       value={stats.totalPips}        color={stats.totalPips >= 0 ? '#00e676' : '#ff4757'} />
    <StatPill label="SIGNALS"    value={stats.signals}          color="#f0b429" />
    <StatPill label="STREAK"     value={stats.streak > 0 ? `+${stats.streak}` : stats.streak} color={stats.streak >= 0 ? '#00e676' : '#ff4757'} />

    {/* Active session badge */}
    {activeSessions.length > 0 && activeSessions.map(ses => (
      <div key={ses.name} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '4px 12px', borderRadius: 4, background: `${ses.color}15`, border: `1px solid ${ses.color}40` }}>
        <span style={{ fontSize: 9, color: '#6b7a8d', fontFamily: "'Space Mono', monospace", letterSpacing: 1 }}>SESSION</span>
        <span style={{ fontSize: 13, fontWeight: 700, color: ses.color, fontFamily: "'Space Mono', monospace" }}>{ses.abbr}</span>
      </div>
    ))}
  </div>
)}
        </header>

        {/* ── CONTENT ── */}
        <div style={s.content}>
          {activeNav === 'chart' || activeNav === 'signals' ? (
            <>
              <div style={s.topRow}>
                <div style={s.chartWrap}>
                  <ChartComponent
                    key={activeSymbol}
                    symbol={activeSymbol}
                    levels={aiData?.keyLevels || { resistance: 0, support: 0, ema: 0 }}
                    visuals={aiData?.visuals || {}}
                    tradeSetup={aiData?.tradeSetup}
                  />
                </div>
                <div style={s.signalWrap}>
                  <SignalCard
                    externalData={aiData}
                    onRefresh={() => runAnalysis(activeSymbol)}
                    loading={loading}
                  />
                </div>
              </div>

              <div style={s.historyWrap}>
                <div style={s.sectionHeader}>
                  <span style={s.sectionTitle}>Verified AI Performance</span>
                  <span style={s.sectionSub}>Live results from backend</span>
                </div>
                <HistoryTable />
              </div>
            </>
          ) : activeNav === 'journal' ? (
            <div style={s.placeholderPage}>
              <span style={s.placeholderIcon}>📓</span>
              <span style={s.placeholderText}>Trade Journal — coming soon</span>
            </div>
          ) : (
            <div style={s.placeholderPage}>
              <span style={s.placeholderIcon}>⚙️</span>
              <span style={s.placeholderText}>Settings — coming soon</span>
            </div>
          )}
        </div>
      </div>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;700;800&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #080c10; font-family: 'Syne', sans-serif; overflow: hidden; }
        ::-webkit-scrollbar { width: 4px; height: 4px; }
        ::-webkit-scrollbar-track { background: #0d1117; }
        ::-webkit-scrollbar-thumb { background: #1e2a38; border-radius: 2px; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
      `}</style>
    </div>
  );
}

const StatPill = ({ label, value, color }) => (
  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '4px 12px', borderRadius: 4, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)' }}>
    <span style={{ fontSize: 9, color: '#6b7a8d', fontFamily: "'Space Mono', monospace", letterSpacing: 1 }}>{label}</span>
    <span style={{ fontSize: 13, fontWeight: 700, color, fontFamily: "'Space Mono', monospace" }}>{value}</span>
  </div>
);

// Converted styles to a function to compute dynamic layout properly
const getStyles = (isMobile, sidebarOpen) => ({
  root: {
    display: 'flex', height: '100vh', width: '100vw',
    overflow: 'hidden', background: '#080c10', color: '#e8edf3',
  },
  mobileOverlay: {
    position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
    background: 'rgba(0,0,0,0.5)', zIndex: 40,
  },
  sidebar: {
    position: isMobile ? 'fixed' : 'relative',
    left: isMobile && !sidebarOpen ? -200 : 0,
    zIndex: 50,
    flexShrink: 0, background: '#0d1117',
    borderRight: '1px solid rgba(255,255,255,0.06)',
    display: 'flex',
    flexDirection: 'column',
    width: sidebarOpen ? 200 : 60,
    height: '100vh',
    transition: 'all 0.3s ease', overflow: 'hidden',
  },
  logoWrap: {
    display: 'flex', alignItems: 'center', gap: 10,
    padding: '18px 14px', cursor: 'pointer',
    borderBottom: '1px solid rgba(255,255,255,0.05)',
    flexShrink: 0,
  },
  logoMark: {
    width: 32, height: 32, borderRadius: 6, background: '#f0b429',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: 16, fontWeight: 800, color: '#080c10', flexShrink: 0,
  },
  logoText: {
    fontSize: 15, fontWeight: 800, color: '#e8edf3',
    letterSpacing: 2, whiteSpace: 'nowrap',
    fontFamily: "'Syne', sans-serif",
  },
  nav: { display: 'flex', flexDirection: 'column', padding: '12px 8px', gap: 2 },
  navBtn: {
    display: 'flex', alignItems: 'center', gap: 10,
    padding: '10px 10px', borderRadius: 6, border: 'none',
    background: 'transparent', cursor: 'pointer', width: '100%',
    transition: 'background 0.15s',
  },
  navBtnActive: { background: 'rgba(240,180,41,0.08)' },
  navLabel: { fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap' },
  sessionBox: {
    marginTop: 'auto', padding: '12px 10px',
    borderTop: '1px solid rgba(255,255,255,0.05)',
    display: 'flex', flexDirection: 'column', gap: 8,
  },
  sessionTitle: {
    fontSize: 9, color: '#3d4d5e', letterSpacing: 2,
    fontFamily: "'Space Mono', monospace", marginBottom: 4,
  },
  sessionRow: { display: 'flex', alignItems: 'center', gap: 8, transition: 'opacity 0.3s' },
  sessionDot: { width: 7, height: 7, borderRadius: '50%', flexShrink: 0, transition: 'box-shadow 0.3s' },
  sessionInfo: { display: 'flex', flexDirection: 'column', gap: 1 },
  clockBox: {
    display: 'flex', alignItems: 'center', gap: 8,
    padding: '10px 12px', borderTop: '1px solid rgba(255,255,255,0.05)',
    flexShrink: 0,
  },
  clockDot: { width: 6, height: 6, borderRadius: '50%', background: '#00e676', flexShrink: 0 },
  clockText: { fontSize: 10, fontFamily: "'Space Mono', monospace", color: '#6b7a8d', whiteSpace: 'nowrap' },
  main: { flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 },
  topbar: {
    display: 'flex', alignItems: 'center', justifyContent: 'flex-start',
    padding: '0 16px', height: 60, flexShrink: 0,
    background: '#0d1117', borderBottom: '1px solid rgba(255,255,255,0.06)',
    gap: 12, overflowX: 'auto', whiteSpace: 'nowrap',
  },
  hamburgerBtn: {
    background: 'transparent', border: 'none', cursor: 'pointer',
    display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 8px 0 0'
  },
  pairTabs: { display: 'flex', gap: 4, flex: 1, overflowX: 'auto', paddingBottom: 2 },
  pairTab: {
    display: 'flex', alignItems: 'center', gap: 5,
    padding: '6px 12px', borderRadius: 4, border: '1px solid transparent',
    background: 'transparent', color: '#6b7a8d', cursor: 'pointer',
    fontSize: 12, fontFamily: "'Space Mono', monospace",
    transition: 'all 0.15s', whiteSpace: 'nowrap', flexShrink: 0,
  },
  pairTabActive: {
    background: 'rgba(240,180,41,0.1)',
    border: '1px solid rgba(240,180,41,0.3)',
    color: '#f0b429',
  },
  pairTabIcon: { fontSize: 13 },
  statsBar: { display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 },
  content: { flex: 1, display: 'flex', flexDirection: 'column', overflowY: 'auto', padding: isMobile ? '12px' : '16px', gap: 16 },
  topRow: {
    display: 'flex',
    flexDirection: isMobile ? 'column' : 'row',
    gap: 16,
    height: isMobile ? 'auto' : 500,
    minHeight: isMobile ? 'auto' : 500,
  },
  chartWrap: {
    flex: isMobile ? 'none' : '0 0 70%',
    height: isMobile ? 350 : '100%', // Prevent collapsing on mobile
    border: '1px solid rgba(255,255,255,0.07)',
    borderRadius: 10, overflow: 'hidden', background: '#161A25',
  },
  signalWrap: { flex: isMobile ? 'none' : '0 0 calc(30% - 16px)' },
  historyWrap: { display: 'flex', flexDirection: 'column', gap: 8, marginTop: isMobile ? 12 : 0 },
  sectionHeader: { display: 'flex', alignItems: 'baseline', gap: 10 },
  sectionTitle: { fontSize: 15, fontWeight: 700, color: '#9ca3af' },
  sectionSub: { fontSize: 11, color: '#3d4d5e', fontFamily: "'Space Mono', monospace" },
  placeholderPage: {
    flex: 1, display: 'flex', flexDirection: 'column',
    alignItems: 'center', justifyContent: 'center', gap: 12,
  },
  placeholderIcon: { fontSize: 40 },
  placeholderText: { fontSize: 14, color: '#3d4d5e', fontFamily: "'Space Mono', monospace" },
});

export default App;