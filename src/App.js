import React, { useState, useEffect } from 'react';
import axios from 'axios'; 
import ChartComponent from './ChartComponent';
import SignalCard from './SignalCard';
import HistoryTable from './HistoryTable';
import './App.css';

// ⚠️ CRITICAL UPDATE: Use the URL from your Render Dashboard
const API_URL = 'https://aura-trade-v1.onrender.com'; 

function App() {
  // 1. STATE: Lifted up so both Chart and SignalCard can use it
  const [aiData, setAiData] = useState(null);
  const [loading, setLoading] = useState(false);

  // 2. THE BRAIN: Fetch Logic moved here
  const runAnalysis = async () => {
    setLoading(true);
    try {
      console.log("🧠 App: Ping AI...");
      // This hits your Node server, which proxies to Python internally
      const res = await axios.post(`${API_URL}/api/analyze`, {
        timeframe: '1h',
        currency: 'USD'
      });
      
      if (res.data) {
        console.log("🧠 Data Received:", res.data);
        setAiData(res.data);
      }
    } catch (err) {
      console.error("Analysis Error:", err);
    } finally {
      setLoading(false);
    }
  };

  // 3. AUTOMATION: Run on load and every 60s
  useEffect(() => {
    runAnalysis(); // Run immediately
    const timer = setInterval(runAnalysis, 60000); // Run every minute
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="dashboard-container" style={{ display: 'flex', height: '100vh', width: '100vw', overflow: 'hidden', backgroundColor: '#0b0e11' }}>
      
      {/* 1. SIDEBAR */}
      <div className="sidebar-desktop" style={{ width: '60px', backgroundColor: '#151920', borderRight: '1px solid #333', display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '20px 0', flexShrink: 0 }}>
        <div style={{ width: '10px', height: '10px', background: '#26a69a', borderRadius: '50%' }}></div>
      </div>

      {/* 2. MAIN PITCH */}
      <div className="main-content" style={{ flex: 1, display: 'flex', flexDirection: 'column', padding: '20px', gap: '20px', overflowY: 'auto' }}>
        
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h1 style={{ margin: 0, fontSize: '24px', color: '#e1e3e6' }}>AuraTrade AI</h1>
        </div>
        
        {/* === TOP ZONE === */}
        <div className="top-section" style={{ display: 'flex', flexDirection: 'row', gap: '20px', height: '500px', minHeight: '500px', width: '100%' }}>
          
          {/* Chart Area */}
          <div className="chart-container" style={{ flex: 0.7, border: '1px solid #333', borderRadius: '12px', overflow: 'hidden' }}>
            <ChartComponent 
               levels={aiData?.keyLevels || { resistance: 0, support: 0, ema: 0 }} 
            />
          </div>

          {/* Signal Area */}
          <div className="signal-card" style={{ flex: 0.3 }}>
            <SignalCard 
               externalData={aiData} 
               onRefresh={runAnalysis} 
               loading={loading}
            />
          </div>

        </div>

        {/* === BOTTOM ZONE === */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '15px' }}>
          <h2 style={{ fontSize: '18px', margin: 0, color: '#9ca3af' }}>Verified AI Performance</h2>
          <div className="trade-history-table">
            <HistoryTable />
          </div>
        </div>

      </div>
    </div>
  );
}

export default App;