import React, { useState, useEffect } from 'react';
import axios from 'axios';

const HistoryTable = () => {
  const [trades, setTrades] = useState([]);

  useEffect(() => {
    const fetchTrades = async () => {
      try {
        const response = await axios.get('http://localhost:5000/api/trades');
        setTrades(response.data);
      } catch (error) {
        console.error("Error fetching trades:", error);
      }
    };

    fetchTrades();
  }, []);

  return (
    <div style={{ 
      backgroundColor: '#151920', 
      borderRadius: '12px', 
      padding: '20px', 
      border: '1px solid #333',
      overflow: 'hidden' 
    }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', color: '#e1e3e6' }}>
        
        <thead>
          <tr style={{ backgroundColor: '#1e2329', textAlign: 'left' }}>
            <th style={{ padding: '12px', fontSize: '13px', color: '#9ca3af' }}>DATE & TIME</th>
            <th style={{ padding: '12px', fontSize: '13px', color: '#9ca3af' }}>PAIR</th>
            <th style={{ padding: '12px', fontSize: '13px', color: '#9ca3af' }}>TYPE</th>
            <th style={{ padding: '12px', fontSize: '13px', color: '#9ca3af' }}>ENTRY</th>
            <th style={{ padding: '12px', fontSize: '13px', color: '#9ca3af' }}>PROFIT</th>
            <th style={{ padding: '12px', fontSize: '13px', color: '#9ca3af' }}>STATUS</th>
          </tr>
        </thead>

        <tbody>
          {trades.length === 0 ? (
            <tr>
              <td colSpan="6" style={{ padding: '20px', textAlign: 'center', color: '#9ca3af' }}>
                Loading match history...
              </td>
            </tr>
          ) : (
            trades.map((trade, index) => {
              // LOGIC: Check if it is a Win
              const isWin = trade.result === 'WON';

              return (
                <tr key={index} style={{ 
                  backgroundColor: index % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)',
                  borderBottom: '1px solid #333' 
                }}>
                  
                  {/* 1. DATE & TIME: Combined */}
                  <td style={{ padding: '12px', fontWeight: 'bold', fontSize: '12px' }}>
                    {new Date(trade.timestamp).toLocaleDateString()} {new Date(trade.timestamp).toLocaleTimeString()}
                  </td>
                  
                  {/* 2. PAIR */}
                  <td style={{ padding: '12px' }}>{trade.pair}</td>
                  
                  {/* 3. TYPE: Buy (Green) or Sell (Red) */}
                  <td style={{ 
                    padding: '12px', 
                    color: trade.action === 'Buy' ? '#26a69a' : '#ef5350' 
                  }}>
                    {trade.action}
                  </td>
                  
                  {/* 4. ENTRY */}
                  <td style={{ padding: '12px' }}>{trade.entry}</td>
                  
                  {/* 5. PROFIT: Shows profit or '-' if missing */}
                  <td style={{ 
                    padding: '12px',
                    color: (trade.profit && trade.profit > 0) ? '#26a69a' : '#ef5350'
                  }}>
                    {trade.profit ? trade.profit : '-'}
                  </td>
                  
                  {/* 6. STATUS: The Badge */}
                  <td style={{ padding: '12px' }}>
                    <span style={{
                      padding: '4px 12px',
                      borderRadius: '20px',
                      fontSize: '12px',
                      fontWeight: 'bold',
                      backgroundColor: isWin ? 'rgba(38, 166, 154, 0.2)' : 'rgba(239, 83, 80, 0.2)',
                      color: isWin ? '#26a69a' : '#ef5350',
                      border: isWin ? '1px solid #26a69a' : '1px solid #ef5350'
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
  );
};

export default HistoryTable;