import React from 'react';
import ChartComponent from './ChartComponent';
import SignalCard from './SignalCard';
import HistoryTable from './HistoryTable';
import './App.css'; // Ensure the CSS is imported!

function App() {
  // NOTE: In a real app, you would lift the 'data' state here 
  // and pass it to both ChartComponent and SignalCard.
  // For now, we are fixing the LAYOUT.

  return (
    // THE STADIUM: Added 'dashboard-container' class
    <div className="dashboard-container" style={{ display: 'flex', height: '100vh', width: '100vw', overflow: 'hidden', backgroundColor: '#0b0e11' }}>
      
      {/* 1. SIDEBAR (Left Wing) */}
      {/* Added 'sidebar-desktop' class so we can hide it on mobile */}
      <div className="sidebar-desktop" style={{ 
        width: '60px', 
        backgroundColor: '#151920', 
        borderRight: '1px solid #333', 
        display: 'flex', 
        flexDirection: 'column', 
        alignItems: 'center', 
        padding: '20px 0',
        flexShrink: 0 
      }}>
        <div style={{ width: '10px', height: '10px', background: '#26a69a', borderRadius: '50%' }}></div>
      </div>

      {/* 2. MAIN PITCH (Right Side) */}
      <div className="main-content" style={{ 
        flex: 1, 
        display: 'flex', 
        flexDirection: 'column', 
        padding: '20px', 
        gap: '20px', 
        overflowY: 'auto' 
      }}>
        
        {/* Header */}
        <h1 style={{ margin: 0, fontSize: '24px', color: '#e1e3e6' }}>AuraTrade AI</h1>
        
        {/* === TOP ZONE (Attack) === */}
        {/* Added 'top-section' class. The CSS will force this to column on mobile. */}
        <div className="top-section" style={{ 
          display: 'flex', 
          flexDirection: 'row', 
          gap: '20px', 
          height: '500px', 
          minHeight: '500px',
          width: '100%'
        }}>
          
          {/* Chart Area */}
          {/* Added 'chart-container' class */}
          <div className="chart-container" style={{ flex: 0.7, border: '1px solid #333', borderRadius: '12px', overflow: 'hidden' }}>
            <ChartComponent />
          </div>

          {/* Signal Area */}
          {/* Added 'signal-card' class */}
          <div className="signal-card" style={{ flex: 0.3 }}>
            {/* Note: You will need to pass chartData={...} here eventually */}
            <SignalCard />
          </div>

        </div>

        {/* === BOTTOM ZONE (Defense) === */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '15px' }}>
          <h2 style={{ fontSize: '18px', margin: 0, color: '#9ca3af' }}>Verified AI Performance</h2>
          
          {/* Added class for table scrolling */}
          <div className="trade-history-table">
            <HistoryTable />
          </div>
        </div>

      </div>

    </div>
  );
}

export default App;