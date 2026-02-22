import React, { useState, useEffect } from 'react';
import axios from 'axios'; 
import ChartComponent from './ChartComponent';
import SignalCard from './SignalCard';
import HistoryTable from './HistoryTable';
import './App.css';

// ☁️ LIVE CLOUD SERVER ADDRESS
const API_URL = 'https://aura-trade-v1.onrender.com'; 

// 🏆 THE ROSTER: Exact ticker mappings required by MongoDB and Python
const ASSETS = [
  { id: 'GC=F', name: 'Gold (XAU/USD)', icon: '🟡' },
  { id: 'EURUSD=X', name: 'EUR/USD', icon: '🇪🇺' },
  { id: 'GBPUSD=X', name: 'GBP/USD', icon: '🇬🇧' },
  { id: 'JPY=X', name: 'USD/JPY', icon: '🇯🇵' },
  { id: 'CHF=X', name: 'USD/CHF', icon: '🇨🇭' },
  { id: 'AUDUSD=X', name: 'AUD/USD', icon: '🇦🇺' },
  { id: 'CAD=X', name: 'USD/CAD', icon: '🇨🇦' },
  { id: 'NZDUSD=X', name: 'NZD/USD', icon: '🇳🇿' }
];

function App() {
  const [aiData, setAiData] = useState(null);
  const [loading, setLoading] = useState(false);
  
  // 🏆 Track the currently selected asset (Defaults to Gold)
  const [activeSymbol, setActiveSymbol] = useState('GC=F');
  const [dropdownOpen, setDropdownOpen] = useState(false);

  // THE BRAIN: Fetch Logic
  const runAnalysis = async (symbolToAnalyze) => {
    setLoading(true);
    try {
      console.log(`🧠 App: Ping AI for ${symbolToAnalyze}...`);
      const res = await axios.post(`${API_URL}/api/analyze`, {
        symbol: symbolToAnalyze, // 👈 Tell the AI Brain exactly which asset to analyze
        timeframe: '1h'
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

  // AUTOMATION: Run immediately on load or when the symbol changes
  useEffect(() => {
    runAnalysis(activeSymbol);
    const timer = setInterval(() => runAnalysis(activeSymbol), 60000); // Auto-refresh every minute
    return () => clearInterval(timer);
  }, [activeSymbol]);

  const selectedAsset = ASSETS.find(a => a.id === activeSymbol) || ASSETS[0];

  return (
    <div className="dashboard-container" style={{ display: 'flex', height: '100vh', width: '100vw', overflow: 'hidden', backgroundColor: '#0b0e11' }}>
      
      {/* 1. SIDEBAR */}
      <div className="sidebar-desktop" style={{ width: '60px', backgroundColor: '#151920', borderRight: '1px solid #333', display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '20px 0', flexShrink: 0 }}>
        <div style={{ width: '10px', height: '10px', background: '#26a69a', borderRadius: '50%' }}></div>
      </div>

      {/* 2. MAIN PITCH */}
      <div className="main-content" style={{ flex: 1, display: 'flex', flexDirection: 'column', padding: '20px', gap: '20px', overflowY: 'auto' }}>
        
        {/* === HEADER & MODERN ASSET SWITCHER === */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', position: 'relative', zIndex: 100 }}>
          <h1 style={{ margin: 0, fontSize: '24px', color: '#e1e3e6' }}>AuraTrade AI</h1>
          
          {/* 🎛️ THE TACTICAL DROPDOWN */}
          <div style={{ position: 'relative' }}>
            <button 
              onClick={() => setDropdownOpen(!dropdownOpen)}
              style={{ display: 'flex', alignItems: 'center', gap: '10px', background: '#151920', border: '1px solid #333', padding: '10px 18px', borderRadius: '8px', color: '#e1e3e6', cursor: 'pointer', fontSize: '15px', fontWeight: 'bold', transition: 'border-color 0.2s' }}
              onMouseOver={(e) => e.currentTarget.style.borderColor = '#26a69a'}
              onMouseOut={(e) => e.currentTarget.style.borderColor = '#333'}
            >
              <span>{selectedAsset.icon} {selectedAsset.name}</span>
              <span style={{ fontSize: '12px', color: '#787b86' }}>▼</span>
            </button>
            
            {dropdownOpen && (
              <div style={{ position: 'absolute', top: '50px', right: '0', background: '#151920', border: '1px solid #333', borderRadius: '8px', width: '200px', overflow: 'hidden', boxShadow: '0 8px 24px rgba(0,0,0,0.8)' }}>
                {ASSETS.map(asset => (
                  <div 
                    key={asset.id} 
                    onClick={() => { 
                        setActiveSymbol(asset.id); 
                        setDropdownOpen(false); 
                    }}
                    style={{ padding: '12px 16px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '12px', color: asset.id === activeSymbol ? '#26a69a' : '#e1e3e6', background: asset.id === activeSymbol ? 'rgba(38, 166, 154, 0.1)' : 'transparent', borderBottom: '1px solid #1e222d', transition: 'background 0.2s' }}
                    onMouseOver={(e) => { if (asset.id !== activeSymbol) e.currentTarget.style.background = '#1e222d'; }}
                    onMouseOut={(e) => { if (asset.id !== activeSymbol) e.currentTarget.style.background = 'transparent'; }}
                  >
                    <span style={{ fontSize: '16px' }}>{asset.icon}</span>
                    <span style={{ fontWeight: asset.id === activeSymbol ? 'bold' : 'normal' }}>{asset.name}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
        
        {/* === TOP ZONE === */}
        <div className="top-section" style={{ display: 'flex', flexDirection: 'row', gap: '20px', height: '500px', minHeight: '500px', width: '100%', zIndex: 1 }}>
          
          {/* Chart Area */}
          <div className="chart-container" style={{ flex: 0.7, border: '1px solid #333', borderRadius: '12px', overflow: 'hidden', background: '#161A25' }}>
            {/* 🛑 CRITICAL FIX: Passed 'currentSymbol' exactly as ChartComponent expects it */}
            <ChartComponent 
               currentSymbol={activeSymbol}
               levels={aiData?.keyLevels || { resistance: 0, support: 0, ema: 0 }} 
               visuals={aiData?.visuals || {}}
               tradeSetup={aiData?.tradeSetup} 
            />
          </div>

          {/* Signal Area */}
          <div className="signal-card" style={{ flex: 0.3 }}>
            <SignalCard 
               externalData={aiData} 
               onRefresh={() => runAnalysis(activeSymbol)} 
               loading={loading}
            />
          </div>

        </div>

        {/* === BOTTOM ZONE === */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '15px', zIndex: 1 }}>
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