import React, { useState, useEffect } from 'react';
import axios from 'axios';

const API_URL = 'https://aura-trade-v1.onrender.com';

const HistoryTable = () => {
  const [trades,   setTrades]   = useState([]);
  const [loading,  setLoading]  = useState(true);
  const [sortKey,  setSortKey]  = useState('timestamp');
  const [sortDir,  setSortDir]  = useState('desc');
  const [filter,   setFilter]   = useState('ALL'); // ALL | WON | LOST

  useEffect(() => {
    const fetch = async () => {
      try {
        const res = await axios.get(`${API_URL}/api/trades`);
        setTrades(res.data);
      } catch (err) {
        console.error('Error fetching trades:', err);
      } finally {
        setLoading(false);
      }
    };
    fetch();
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
    if (filter === 'ALL') return true;
    if (filter === 'WON')  return t.result === 'WON';
    if (filter === 'LOST') return t.result !== 'WON';
    return true;
  });

  const sorted = [...filtered].sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    if (sortKey === 'timestamp') { av = new Date(av); bv = new Date(bv); }
    if (sortKey === 'profit') { av = parseFloat(av) || 0; bv = parseFloat(bv) || 0; }
    if (av < bv) return sortDir === 'asc' ? -1 : 1;
    if (av > bv) return sortDir === 'asc' ? 1 : -1;
    return 0;
  });

  // Stats summary
  const wins       = trades.filter(t => t.result === 'WON').length;
  const losses     = trades.length - wins;
  const winRate    = trades.length > 0 ? Math.round((wins / trades.length) * 100) : 0;
  const totalPips  = trades.reduce((sum, t) => sum + (parseFloat(t.profit) || 0), 0);
  const avgProfit  = trades.length > 0 ? (totalPips / trades.length).toFixed(1) : 0;
  const bestTrade  = trades.length > 0 ? Math.max(...trades.map(t => parseFloat(t.profit) || 0)) : 0;

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

  return (
    <div style={{ background: '#0d1117', borderRadius: 10, border: '1px solid rgba(255,255,255,0.07)', overflow: 'hidden', fontFamily: "'Syne', sans-serif" }}>

      {/* Stats summary bar */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
        {[
          { label: 'TOTAL',    value: trades.length,           color: '#9ca3af' },
          { label: 'WINS',     value: wins,                    color: '#00e676' },
          { label: 'LOSSES',   value: losses,                  color: '#ff4757' },
          { label: 'WIN RATE', value: `${winRate}%`,           color: winRate >= 60 ? '#00e676' : winRate >= 45 ? '#f0b429' : '#ff4757' },
          { label: 'TOTAL PIPS', value: `${totalPips > 0 ? '+' : ''}${parseFloat(totalPips).toFixed(1)}`, color: totalPips >= 0 ? '#00e676' : '#ff4757' },
          { label: 'BEST',     value: `+${bestTrade.toFixed(1)}`, color: '#f0b429' },
        ].map((stat, i) => (
          <div key={i} style={{ padding: '10px 14px', borderRight: i < 5 ? '1px solid rgba(255,255,255,0.05)' : 'none', textAlign: 'center' }}>
            <div style={{ fontSize: 9, color: '#3d4d5e', fontFamily: "'Space Mono', monospace", letterSpacing: 1, marginBottom: 4 }}>{stat.label}</div>
            <div style={{ fontSize: 15, fontWeight: 700, color: stat.color, fontFamily: "'Space Mono', monospace" }}>{stat.value}</div>
          </div>
        ))}
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 16px', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
        <span style={{ fontSize: 11, color: '#6b7a8d', fontFamily: "'Space Mono', monospace" }}>
          {sorted.length} records
        </span>
        <div style={{ display: 'flex', gap: 4 }}>
          {['ALL', 'WON', 'LOST'].map(f => (
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
                { key: 'profit',    label: 'PROFIT' },
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
              <tr><td colSpan="6" style={{ padding: 24, textAlign: 'center', color: '#3d4d5e', fontFamily: "'Space Mono', monospace", fontSize: 11 }}>Loading history...</td></tr>
            ) : sorted.length === 0 ? (
              <tr><td colSpan="6" style={{ padding: 24, textAlign: 'center', color: '#3d4d5e', fontFamily: "'Space Mono', monospace", fontSize: 11 }}>No trades found.</td></tr>
            ) : (
              sorted.map((trade, i) => {
                const isWin = trade.result === 'WON';
                const isBuy = trade.action === 'Buy';
                const profit = parseFloat(trade.profit) || 0;
                return (
                  <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.04)', transition: 'background 0.1s' }}
                    onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.02)'}
                    onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                    <td style={{ padding: '13px 16px', color: '#6b7a8d', whiteSpace: 'nowrap', fontFamily: "'Space Mono', monospace", fontSize: 11 }}>
                      {formatDate(trade.timestamp)}
                    </td>
                    <td style={{ padding: '13px 16px', color: '#e8edf3', fontWeight: 700, fontFamily: "'Space Mono', monospace", fontSize: 12 }}>
                      {trade.pair}
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
                    <td style={{ padding: '13px 16px', color: '#9ca3af', fontFamily: "'Space Mono', monospace", fontSize: 11 }}>
                      {trade.entry}
                    </td>
                    <td style={{ padding: '13px 16px', fontWeight: 700, fontFamily: "'Space Mono', monospace", fontSize: 12, color: profit > 0 ? '#00e676' : profit < 0 ? '#ff4757' : '#9ca3af' }}>
                      {profit > 0 ? '+' : ''}{profit.toFixed(1)}
                    </td>
                    <td style={{ padding: '13px 16px', textAlign: 'right' }}>
                      <span style={{
                        fontSize: 10, padding: '3px 10px', borderRadius: 12, fontWeight: 700,
                        fontFamily: "'Space Mono', monospace", letterSpacing: 0.5,
                        background: isWin ? 'rgba(0,230,118,0.1)' : 'rgba(255,71,87,0.1)',
                        color: isWin ? '#00e676' : '#ff4757',
                        border: `1px solid ${isWin ? 'rgba(0,230,118,0.2)' : 'rgba(255,71,87,0.2)'}`,
                      }}>
                        {trade.result}
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