import React, { useState, useEffect } from 'react';
import axios from 'axios';

// ⚠️ CRITICAL UPDATE: Use your NEW Render Monolith URL here
const API_URL = 'https://aura-trade-v1.onrender.com';

const HistoryTable = () => {
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchTrades = async () => {
      try {
        // ✅ UPDATED: Use the variable instead of hardcoding
        const response = await axios.get(`${API_URL}/api/trades`);
        setTrades(response.data);
      } catch (error) {
        console.error("Error fetching trades:", error);
      } finally {
        setLoading(false);
      }
    };

    fetchTrades();
  }, []);

  // Format Date: "Oct 10, 2023 1:00 AM"
  const formatDate = (isoString) => {
    if (!isoString) return '-';
    return new Date(isoString).toLocaleString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric',
      hour: 'numeric', minute: '2-digit', hour12: true
    });
  };

  // --- STYLES ---
  const styles = {
    // ... (Your styles remain exactly the same) ...
    // I am omitting them here to save space, but DO NOT delete them from your file!
    container: {
      width: '100%',
      backgroundColor: '#131722', 
      borderRadius: '8px',
      border: '1px solid #2a2e39',
      padding: '20px',
      marginTop: '20px',
      boxShadow: '0 4px 6px rgba(0, 0, 0, 0.3)',
      overflowX: 'auto' 
    },
    headerText: {
      color: '#d1d5db',
      fontSize: '18px',
      fontWeight: '600',
      marginBottom: '15px',
      letterSpacing: '0.5px'
    },
    table: {
      width: '100%',
      borderCollapse: 'collapse',
      fontSize: '14px',
      textAlign: 'left'
    },
    th: {
      color: '#6b7280', 
      textTransform: 'uppercase',
      fontSize: '12px',
      fontWeight: '600',
      padding: '12px 16px', 
      borderBottom: '1px solid #2a2e39'
    },
    td: {
      padding: '16px', 
      color: '#d1d5db', 
      borderBottom: '1px solid #2a2e39'
    },
    badgeWin: {
      backgroundColor: 'rgba(16, 185, 129, 0.1)',
      color: '#34d399',
      border: '1px solid rgba(16, 185, 129, 0.2)',
      padding: '4px 10px',
      borderRadius: '20px',
      fontSize: '11px',
      fontWeight: 'bold',
      display: 'inline-block'
    },
    badgeLoss: {
      backgroundColor: 'rgba(244, 63, 94, 0.1)',
      color: '#fb7185',
      border: '1px solid rgba(244, 63, 94, 0.2)',
      padding: '4px 10px',
      borderRadius: '20px',
      fontSize: '11px',
      fontWeight: 'bold',
      display: 'inline-block'
    }
  };

  return (
    <div style={styles.container}>
      
      <table style={styles.table}>
        <thead>
          <tr>
            <th style={styles.th}>Date & Time</th>
            <th style={styles.th}>Pair</th>
            <th style={styles.th}>Type</th>
            <th style={styles.th}>Entry</th>
            <th style={styles.th}>Profit</th>
            <th style={{ ...styles.th, textAlign: 'right' }}>Status</th>
          </tr>
        </thead>
        
        <tbody>
          {loading ? (
            <tr>
              <td colSpan="6" style={{ padding: '20px', textAlign: 'center', color: '#6b7280' }}>
                Loading History...
              </td>
            </tr>
          ) : trades.length === 0 ? (
            <tr>
              <td colSpan="6" style={{ padding: '20px', textAlign: 'center', color: '#6b7280' }}>
                No trades found.
              </td>
            </tr>
          ) : (
            trades.map((trade, index) => {
              const isWin = trade.result === 'WON';
              const isBuy = trade.action === 'Buy';

              return (
                <tr key={index} style={{ borderBottom: '1px solid #2a2e39' }}>
                  <td style={{ ...styles.td, whiteSpace: 'nowrap', fontWeight: '500' }}>
                    {formatDate(trade.timestamp)}
                  </td>
                  <td style={{ ...styles.td, color: '#9ca3af' }}>
                    {trade.pair}
                  </td>
                  <td style={{ ...styles.td, color: isBuy ? '#34d399' : '#fb7185', fontWeight: 'bold' }}>
                    {trade.action}
                  </td>
                  <td style={styles.td}>
                    {trade.entry}
                  </td>
                  <td style={{ ...styles.td, color: trade.profit > 0 ? '#34d399' : '#fb7185', fontWeight: 'bold' }}>
                    {trade.profit > 0 ? '+' : ''}{trade.profit}
                  </td>
                  <td style={{ ...styles.td, textAlign: 'right' }}>
                    <span style={isWin ? styles.badgeWin : styles.badgeLoss}>
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
  );
};

export default HistoryTable;