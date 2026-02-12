import React, { useState, useEffect } from 'react';
import axios from 'axios';
import ChartComponent from './ChartComponent';
import SignalCard from './SignalCard';
import HistoryTable from './HistoryTable';
// import './App.css'; // Uncomment if you have this file

// ☁️ API URL
const API_URL = 'https://aura-trade.onrender.com';

function App() {
  // 1. STATE: Lifted up so both Chart and SignalCard can use it
  const [aiData, setAiData] = useState(null);
  const [loading, setLoading] = useState(false);

  // 2. THE BRAIN: Fetch Logic
  const runAnalysis = async () => {
    setLoading(true);
    try {
      console.log("🧠 App: Pinging AI Brain...");
      
      // NOTE: If your backend needs Candle Data to find patterns, 
      // you might need to pass candles here in the future.
      const res = await axios.post(`${API_URL}/api/analyze`, {
        timeframe: '1h',
        currency: 'USD',
        pair: 'XAUUSD' // Added Pair so backend knows what to look at
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
    runAnalysis(); 
    const timer = setInterval(runAnalysis, 60000); 
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="dashboard-container" style={{ display: 'flex', height: '100vh', width: '100vw', overflow: 'hidden', backgroundColor: '#0b0e11' }}>
      
      {/* 1. SIDEBAR */}
      <div className="sidebar-desktop" style={{ width: '60px', backgroundColor: '#151920', borderRight: '1px solid #333', display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '20px 0', flexShrink: 0 }}>
        <div style={{ width: '10px', height: '10px', background: '#26a69a', borderRadius: '50%', boxShadow: '0 0 10px #26a69a' }}></div>
      </div>

      {/* 2. MAIN PITCH */}
      <div className="main-content" style={{ flex: 1, display: 'flex', flexDirection: 'column', padding: '20px', gap: '20px', overflowY: 'auto' }}>
        
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h1 style={{ margin: 0, fontSize: '24px', color: '#e1e3e6', letterSpacing: '1px' }}>AuraTrade AI</h1>
          <div style={{ fontSize: '12px', color: '#6b7280' }}>v1.0.4 Live Connection</div>
        </div>
        
        {/* === TOP ZONE === */}
        <div className="top-section" style={{ display: 'flex', flexDirection: 'row', gap: '20px', height: '500px', minHeight: '500px', width: '100%' }}>
          
          {/* Chart Area */}
          <div className="chart-container" style={{ flex: 0.7, border: '1px solid #333', borderRadius: '12px', overflow: 'hidden', position: 'relative' }}>
            {/* 👇 PASSING THE 'EYES' (Key Levels) TO THE CHART */}
            <ChartComponent 
               levels={aiData?.keyLevels || []} 
            />
          </div>

          {/* Signal Area */}
          <div className="signal-card" style={{ flex: 0.3 }}>
            {/* 👇 PASSING THE 'BRAIN' (Analysis) TO THE CARD */}
            <SignalCard 
               externalData={aiData} 
               onRefresh={runAnalysis} 
               loading={loading}
            />
          </div>

        </div>

        {/* === BOTTOM ZONE === */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '15px' }}>
          <HistoryTable />
        </div>

      </div>
    </div>
  );
}

export default App;