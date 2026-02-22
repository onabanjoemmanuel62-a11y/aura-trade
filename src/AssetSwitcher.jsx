import React, { useState, useRef, useEffect } from 'react';

const ASSETS = [
  { symbol: 'GC=F', name: 'Gold' },
  { symbol: 'EURUSD=X', name: 'EUR/USD' },
  { symbol: 'GBPUSD=X', name: 'GBP/USD' },
  { symbol: 'JPY=X', name: 'USD/JPY' },
  { symbol: 'AUDUSD=X', name: 'AUD/USD' },
  { symbol: 'CAD=X', name: 'USD/CAD' },
  { symbol: 'CHF=X', name: 'USD/CHF' },
  { symbol: 'NZDUSD=X', name: 'NZD/USD' }
];

export default function AssetSwitcher({ selectedSymbol, onSymbolChange }) {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef(null);
  
  const activeAsset = ASSETS.find(a => a.symbol === selectedSymbol) || ASSETS[0];

  useEffect(() => {
    const handleClickOutside = (event) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target)) setIsOpen(false);
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  return (
    <div ref={dropdownRef} style={{ position: 'relative', display: 'inline-block', width: '160px', zIndex: 100 }}>
      {/* 🔘 THE MAIN BUTTON */}
      <div
        onClick={() => setIsOpen(!isOpen)}
        style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          backgroundColor: '#1E222D', color: '#D9D9D9', padding: '10px 16px',
          borderRadius: '8px', cursor: 'pointer', border: '1px solid #434651',
          fontWeight: '600', fontSize: '14px', transition: 'all 0.2s ease',
          boxShadow: '0 4px 6px rgba(0,0,0,0.3)'
        }}
        onMouseEnter={(e) => e.currentTarget.style.borderColor = '#26a69a'}
        onMouseLeave={(e) => e.currentTarget.style.borderColor = '#434651'}
      >
        <span>{activeAsset.name}</span>
        <span style={{ 
            transform: isOpen ? 'rotate(180deg)' : 'rotate(0deg)', 
            transition: 'transform 0.2s ease',
            fontSize: '10px', color: '#9ca3af'
        }}>▼</span>
      </div>

      {/* 🔽 THE DROPDOWN MENU */}
      {isOpen && (
        <div style={{
          position: 'absolute', top: '110%', left: 0, right: 0,
          backgroundColor: '#1E222D', border: '1px solid #434651',
          borderRadius: '8px', overflow: 'hidden', boxShadow: '0 10px 25px rgba(0,0,0,0.7)'
        }}>
          {ASSETS.map((asset) => (
            <div
              key={asset.symbol}
              onClick={() => {
                onSymbolChange(asset.symbol);
                setIsOpen(false);
              }}
              style={{
                padding: '12px 16px', cursor: 'pointer', fontSize: '14px', fontWeight: '500',
                color: activeAsset.symbol === asset.symbol ? '#26a69a' : '#D9D9D9',
                backgroundColor: activeAsset.symbol === asset.symbol ? 'rgba(38, 166, 154, 0.1)' : 'transparent',
                borderBottom: '1px solid #2B2F3A', transition: 'background 0.1s'
              }}
              onMouseEnter={(e) => {
                  if (activeAsset.symbol !== asset.symbol) e.currentTarget.style.backgroundColor = '#2B2F3A';
              }}
              onMouseLeave={(e) => {
                  if (activeAsset.symbol !== asset.symbol) e.currentTarget.style.backgroundColor = 'transparent';
              }}
            >
              {asset.name}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}