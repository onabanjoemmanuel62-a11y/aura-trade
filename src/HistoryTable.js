import React, { useState, useEffect } from 'react';
import axios from 'axios';

const API_URL = 'https://aura-trade-v1.onrender.com';

// Helper: A trade is only truly OPEN if its result is not already WON or LOST
const isTradeOpen = (t) => {
  const res = (t.result || '').toUpperCase();
  const st  = (t.status || '').toUpperCase();
  if (res === 'WON' || res === 'LOST') return false;
  return st === 'OPEN' || res === 'PENDING' || !res;
};

const normalizePips = (trade) => {
  const raw = parseFloat(trade.profit);
  if (isNaN(raw) || raw === 0) return 0;
  const sym = (trade.symbol || trade.pair || '').toUpperCase();
  if (Math.abs(raw) < 0.25 && !sym.includes('GC=F') && !sym.includes('XAU')) {
    if (sym.includes('JPY')) return parseFloat((raw * 100).toFixed(1));
    return parseFloat((raw * 10000).toFixed(1));
  }
  return parseFloat(raw.toFixed(1));
};

const HistoryTable = () => {
  const [trades,  setTrades]  = useState([]);
  const [loading, setLoading] = useState(true);
  const [sortKey, setSortKey] = useState('timestamp');
  const [sortDir, setSortDir] = useState('desc');
  const [filter,  setFilter]  = useState('ALL'); // ALL | OPEN | WON | LOST

  useEffect(() => {
    const fetchTrades = async () => {
      try {
        const res = await axios.get(`${API_URL}/api/trades`);
        if (Array.isArray(res.data)) setTrades(res.data);
      } catch (err) {
        console.error('Error fetching trades:', err);
      } finally {
        setLoading(false);
      }
    };
    fetchTrades();
    const id = setInterval(fetchTrades, 30000);
    return () => clearInterval(id);
  }, []);

  const formatDate = (iso) => {
    if (!iso) return '-';
    return new Date(iso).toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit', hour12: true
    });
  };

  const handleSort = (key) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  };

  const filtered = trades.filter(t => {
    const open   = isTradeOpen(t);
    const result = (t.result || '').toUpperCase();
    if (filter === 'ALL')  return true;
    if (filter === 'OPEN') return open;
    if (filter === 'WON')  return !open && result === 'WON';
    if (filter === 'LOST') return !open && result === 'LOST';
    return true;
  });

  const sorted = [...filtered].sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    if (sortKey === 'timestamp') { av = new Date(av || a.createdAt || 0); bv = new Date(bv || b.createdAt || 0); }
    if (sortKey === 'profit') { av = normalizePips(a); bv = normalizePips(b); }
    if (av < bv) return sortDir === 'asc' ? -1 : 1;
    if (av > bv) return sortDir === 'asc' ? 1 : -1;
    return 0;
  });

  // Accurate Stats Summary (with Pips Won, Pips Lost, and Net Pips)
  const openTrades   = trades.filter(t => isTradeOpen(t));
  const closedTrades = trades.filter(t => !isTradeOpen(t));
  const wins         = closedTrades.filter(t => (t.result || '').toUpperCase() === 'WON').length;
  const losses       = closedTrades.filter(t => (t.result || '').toUpperCase() === 'LOST').length;
  const winRate      = closedTrades.length > 0 ? Math.round((wins / closedTrades.length) * 100) : 0;

  const pipsWon = closedTrades.reduce((sum, t) => {
    const p = normalizePips(t);
    return p > 0 ? sum + p : sum;
  }, 0);

  const pipsLost = closedTrades.reduce((sum, t) => {
    const p = normalizePips(t);
    return p < 0 ? sum + Math.abs(p) : sum;
  }, 0);

  const netPips   = pipsWon - pipsLost;
  const bestTrade = closedTrades.length > 0 ? Math.max(0, ...closedTrades.map(t => normalizePips(t))) : 0;

  const SortIcon = ({ col }) => {
    if (sortKey !== col) return <span style={{ color: '#3d4d5e', marginLeft: 4 }}>↕</span>;
    return <span style={{ color: '#f0b429', marginLeft: 4 }}>{sortDir === 'asc' ? '↑' : '↓'}</span>;
  };

  const filterBtnStyle = (f) => ({
    padding: '4px 12px', borderRadius: 4, fontSize: 10, cursor: 'pointer',
    fontFamily: "'Space Mono', monospace", fontWeight: 700, letterSpacing: 0.5,
    border: `1px solid ${filter === f ? 'rgba(240,180,41,0.4)' : 'rgba(255,255,255,0.07)'}`,
    background: filter === f ? 'rgba(240,180,41,0.1)' : 'transparent',
    color: filter === f ? '#f0b429' : '#6b7a8d',
    transition: 'all 0.15s',
  });

  const summaryStats = [
    { label: 'SIGNALS',     value: trades.length,     color: '#9ca3af' },
    { label: 'ACTIVE',      value: openTrades.length, color: '#2962FF' },
    { label: 'WINS / LOSS', value: `${wins}W / ${losses}L`, color: '#00e676' },
    { label: 'WIN RATE',    value: closedTrades.length > 0 ? `${winRate}%` : '—', color: winRate >= 60 ? '#00e676' : winRate >= 45 ? '#f0b429' : '#9ca3af' },
    { label: 'PIPS WON',    value: `+${pipsWon.toFixed(1)}p`, color: '#00e676' },
    { label: 'PIPS LOST',   value: `-${pipsLost.toFixed(1)}p`, color: '#ff4757' },
    { label: 'NET PIPS',    value: `${netPips >= 0 ? '+' : ''}${netPips.toFixed(1)}p`, color: netPips >= 0 ? '#00e676' : '#ff4757' },
    { label: 'BEST TRADE',  value: `+${bestTrade.toFixed(1)}p`, color: '#f0b429' },
  ];

  return (
    <div style={{ background: '#0d1117', borderRadius: 10, border: '1px solid rgba(255,255,255,0.07)', overflow: 'hidden', fontFamily: "'Syne', sans-serif" }}>

      {/* Stats summary bar */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
        {summaryStats.map((stat, i) => (
          <div key={i} style={{ padding: '10px 12px', borderRight: i < summaryStats.length - 1 ? '1px solid rgba(255,255,255,0.05)' : 'none', textAlign: 'center' }}>
            <div style={{ fontSize: 9, color: '#3d4d5e', fontFamily: "'Space Mono', monospace", letterSpacing: 1, marginBottom: 4 }}>{stat.label}</div>
            <div style={{ fontSize: 14, fontWeight: 700, color: stat.color, fontFamily: "'Space Mono', monospace" }}>{stat.value}</div>
          </div>
        ))}
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 16px', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
        <span style={{ fontSize: 11, color: '#6b7a8d', fontFamily: "'Space Mono', monospace" }}>
          {sorted.length} records ({openTrades.length} active, {closedTrades.length} closed)
        </span>
        <div style={{ display: 'flex', gap: 4 }}>
          {['ALL', 'OPEN', 'WON', 'LOST'].map(f => (
            <button key={f} onClick={() => setFilter(f)} style={filterBtnStyle(f)}>{f}</button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
              {[
                { key: 'timestamp', label: 'DATE & TIME' },
                { key: 'pair',      label: 'PAIR' },
                { key: 'action',    label: 'TYPE' },
                { key: 'entry',     label: 'ENTRY' },
                { key: 'sl',        label: 'SL / TP' },
                { key: 'profit',    label: 'PIPS' },
                { key: 'result',    label: 'STATUS', align: 'right' },
              ].map(col => (
                <th
                  key={col.key}
                  onClick={() => handleSort(col.key)}
                  style={{
                    padding: '10px 16px', color: sortKey === col.key ? '#f0b429' : '#3d4d5e',
                    textTransform: 'uppercase', fontSize: 10, fontWeight: 600,
                    fontFamily: "'Space Mono', monospace", letterSpacing: 1,
                    textAlign: col.align || 'left', cursor: 'pointer',
                    userSelect: 'none', transition: 'color 0.15s',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {col.label}<SortIcon col={col.key} />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan="7" style={{ padding: 24, textAlign: 'center', color: '#3d4d5e', fontFamily: "'Space Mono', monospace", fontSize: 11 }}>Loading history...</td></tr>
            ) : sorted.length === 0 ? (
              <tr><td colSpan="7" style={{ padding: 24, textAlign: 'center', color: '#3d4d5e', fontFamily: "'Space Mono', monospace", fontSize: 11 }}>No trades found.</td></tr>
            ) : (
              sorted.map((trade, i) => {
                const isOpen = isTradeOpen(trade);
                const isWin  = !isOpen && (trade.result || '').toUpperCase() === 'WON';
                const isBuy  = (trade.action || '').toUpperCase() === 'BUY';
                const pips   = normalizePips(trade);
                const isBE   = !isOpen && isWin && pips > 0 && pips <= 2.5;

                const badgeBg     = isOpen ? 'rgba(41,98,255,0.15)' : isBE ? 'rgba(240,180,41,0.12)' : isWin ? 'rgba(0,230,118,0.1)' : 'rgba(255,71,87,0.1)';
                const badgeColor  = isOpen ? '#82b1ff'              : isBE ? '#f0b429'               : isWin ? '#00e676'            : '#ff4757';
                const badgeBorder = isOpen ? 'rgba(41,98,255,0.4)'  : isBE ? 'rgba(240,180,41,0.35)' : isWin ? 'rgba(0,230,118,0.2)': 'rgba(255,71,87,0.2)';
                const badgeText   = isOpen ? 'ACTIVE 🟢'            : isBE ? 'WON (BE 🛡️)'           : (trade.result || 'CLOSED');

                return (
                  <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.04)', transition: 'background 0.1s' }}
                    onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.02)'}
                    onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                    <td style={{ padding: '13px 16px', color: '#6b7a8d', whiteSpace: 'nowrap', fontFamily: "'Space Mono', monospace", fontSize: 11 }}>
                      {formatDate(trade.timestamp || trade.createdAt)}
                    </td>
                    <td style={{ padding: '13px 16px', color: '#e8edf3', fontWeight: 700, fontFamily: "'Space Mono', monospace", fontSize: 12 }}>
                      {trade.pair || trade.symbol}
                    </td>
                    <td style={{ padding: '13px 16px' }}>
                      <span style={{
                        fontSize: 10, fontWeight: 700, padding: '3px 9px', borderRadius: 3,
                        fontFamily: "'Space Mono', monospace", letterSpacing: 0.5,
                        background: isBuy ? 'rgba(0,230,118,0.1)' : 'rgba(255,71,87,0.1)',
                        color: isBuy ? '#00e676' : '#ff4757',
                        border: `1px solid ${isBuy ? 'rgba(0,230,118,0.2)' : 'rgba(255,71,87,0.2)'}`,
                      }}>
                        {trade.action?.toUpperCase()}
                      </span>
                    </td>
                    <td style={{ padding: '13px 16px', color: '#e8edf3', fontFamily: "'Space Mono', monospace", fontSize: 11, fontWeight: 700 }}>
                      {trade.entry}
                    </td>
                    <td style={{ padding: '13px 16px', color: '#9ca3af', fontFamily: "'Space Mono', monospace", fontSize: 10.5 }}>
                      <span style={{ color: '#ff4757' }}>{trade.sl ?? '—'}</span>
                      {' / '}
                      <span style={{ color: '#00e676' }}>{trade.tp ?? '—'}</span>
                    </td>
                    <td style={{ padding: '13px 16px', fontWeight: 700, fontFamily: "'Space Mono', monospace", fontSize: 12, color: isOpen ? '#f0b429' : pips > 0 ? '#00e676' : pips < 0 ? '#ff4757' : '#9ca3af' }}>
                      {isOpen ? 'RUNNING' : `${pips > 0 ? '+' : ''}${pips.toFixed(1)}p`}
                    </td>
                    <td style={{ padding: '13px 16px', textAlign: 'right' }}>
                      <span style={{
                        fontSize: 10, padding: '3px 10px', borderRadius: 12, fontWeight: 700,
                        fontFamily: "'Space Mono', monospace", letterSpacing: 0.5,
                        background: badgeBg,
                        color: badgeColor,
                        border: `1px solid ${badgeBorder}`,
                      }}>
                        {badgeText}
                      </span>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;700;800&display=swap');
      `}</style>
    </div>
  );
};

export default HistoryTable;