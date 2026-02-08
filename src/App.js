import React from 'react';
import ChartComponent from './ChartComponent';
import SignalCard from './SignalCard';
import HistoryTable from './HistoryTable';

function App() {
  return (
    // THE STADIUM: Full screen container
    <div style={{ display: 'flex', height: '100vh', width: '100vw', overflow: 'hidden', backgroundColor: '#0b0e11' }}>
      
      {/* 1. SIDEBAR (Left Wing) */}
      <div style={{ 
        width: '60px', 
        backgroundColor: '#151920', 
        borderRight: '1px solid #333', 
        display: 'flex', 
        flexDirection: 'column', 
        alignItems: 'center', 
        padding: '20px 0',
        flexShrink: 0 // Prevents sidebar from squishing
      }}>
        <div style={{ width: '10px', height: '10px', background: '#26a69a', borderRadius: '50%' }}></div>
      </div>

      {/* 2. MAIN PITCH (Right Side) */}
      <div style={{ 
        flex: 1, 
        display: 'flex', 
        flexDirection: 'column', // Stack items top-to-bottom
        padding: '20px', 
        gap: '20px', // Adds 20px breathing room between EVERY section
        overflowY: 'auto' // Allows scrolling if the screen is too small
      }}>
        
        {/* Header */}
        <h1 style={{ margin: 0, fontSize: '24px', color: '#e1e3e6' }}>AuraTrade AI</h1>
        
        {/* === TOP ZONE (Attack) === */}
        {/* Wrapper for Chart + Signal. We give it a FIXED height so it doesn't collapse. */}
        <div style={{ 
          display: 'flex', 
          flexDirection: 'row', 
          gap: '20px', 
          height: '500px', // Force this zone to be exactly 500px tall
          minHeight: '500px',
          width: '100%'
        }}>
          
          {/* Chart Area */}
          <div style={{ flex: 0.7, border: '1px solid #333', borderRadius: '12px', overflow: 'hidden' }}>
            <ChartComponent />
          </div>

          {/* Signal Area */}
          <div style={{ flex: 0.3 }}>
            <SignalCard />
          </div>

        </div>

        {/* === BOTTOM ZONE (Defense) === */}
        {/* History Table. It sits naturally below because of the parent 'column' direction */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '15px' }}>
          <h2 style={{ fontSize: '18px', margin: 0, color: '#9ca3af' }}>Verified AI Performance</h2>
          <HistoryTable />
        </div>

      </div>

    </div>
  );
}

export default App;