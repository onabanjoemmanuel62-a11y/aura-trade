import React, { useState, useRef, useEffect } from 'react';

// The full 8-man roster mapping database symbols to clean UI names
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

  // Find the currently active asset to display its readable name
  const activeAsset = ASSETS.find(a => a.symbol === selectedSymbol) || ASSETS[0];

  // Close dropdown if the user clicks outside the tactical board
  useEffect(() => {
    const handleClickOutside = (event) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  return (
    <div className="relative inline-block text-left w-48" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center justify-between w-full px-4 py-2 text-sm font-medium text-white bg-slate-800 border border-slate-700 rounded-md shadow-sm hover:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        <span>{activeAsset.name}</span>
        <svg className="w-4 h-4 ml-2 -mr-1 text-gray-400" viewBox="0 0 20 20" fill="currentColor">
          <path fillRule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clipRule="evenodd" />
        </svg>
      </button>

      {isOpen && (
        <div className="absolute z-50 w-full mt-2 origin-top-right bg-slate-800 border border-slate-700 rounded-md shadow-lg ring-1 ring-black ring-opacity-5 focus:outline-none">
          <div className="py-1">
            {ASSETS.map((asset) => (
              <button
                key={asset.symbol}
                onClick={() => {
                  onSymbolChange(asset.symbol);
                  setIsOpen(false); // Close the menu after substituting
                }}
                className={`block w-full px-4 py-2 text-sm text-left ${
                  activeAsset.symbol === asset.symbol
                    ? 'bg-blue-600 text-white' // Highlight the active player
                    : 'text-gray-200 hover:bg-slate-700 hover:text-white'
                }`}
              >
                {asset.name}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}