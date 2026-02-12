import React, { useState, useEffect } from 'react';
import axios from 'axios';

const HistoryTable = () => {
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchTrades = async () => {
      try {
        const response = await axios.get('https://aura-trade.onrender.com/api/trades');
        setTrades(response.data);
      } catch (error) {
        console.error("Error fetching trades:", error);
      } finally {
        setLoading(false);
      }
    };

    fetchTrades();
  }, []);

  // Helper to format date like "Oct 10, 2023 1:00 AM"
  const formatDate = (isoString) => {
    if (!isoString) return '-';
    const date = new Date(isoString);
    return date.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true
    });
  };

  return (
    <div className="w-full bg-[#131722] p-6 rounded-lg border border-gray-800 shadow-xl">
      {/* Header */}
      <h2 className="text-gray-300 text-lg font-semibold mb-6 tracking-wide">
        Verified AI Performance
      </h2>

      {/* Table Container */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          {/* Table Head */}
          <thead className="text-xs text-gray-500 uppercase border-b border-gray-800">
            <tr>
              <th scope="col" className="px-4 py-3 font-semibold">Date & Time</th>
              <th scope="col" className="px-4 py-3 font-semibold">Pair</th>
              <th scope="col" className="px-4 py-3 font-semibold">Type</th>
              <th scope="col" className="px-4 py-3 font-semibold">Entry</th>
              <th scope="col" className="px-4 py-3 font-semibold">Profit</th>
              <th scope="col" className="px-4 py-3 font-semibold text-right">Status</th>
            </tr>
          </thead>

          {/* Table Body */}
          <tbody className="divide-y divide-gray-800">
            {loading ? (
              <tr>
                <td colSpan="6" className="px-4 py-8 text-center text-gray-500 animate-pulse">
                  Syncing with AuraTrade Live Server...
                </td>
              </tr>
            ) : trades.length === 0 ? (
              <tr>
                <td colSpan="6" className="px-4 py-8 text-center text-gray-500">
                  No trade history found.
                </td>
              </tr>
            ) : (
              trades.map((trade, index) => {
                // Determine Win/Loss logic based on your API's 'result'
                const isWin = trade.result === 'WON';
                const isBuy = trade.action === 'Buy';

                return (
                  <tr key={index} className="hover:bg-gray-800/30 transition-colors">
                    
                    {/* 1. Date */}
                    <td className="px-4 py-4 font-medium text-gray-300 whitespace-nowrap">
                      {formatDate(trade.timestamp)}
                    </td>

                    {/* 2. Pair */}
                    <td className="px-4 py-4 text-gray-300">
                      {trade.pair}
                    </td>

                    {/* 3. Type (Buy/Sell) */}
                    <td className={`px-4 py-4 font-medium ${isBuy ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {trade.action}
                    </td>

                    {/* 4. Entry */}
                    <td className="px-4 py-4 text-gray-300">
                      {trade.entry}
                    </td>

                    {/* 5. Profit */}
                    <td className={`px-4 py-4 font-medium ${
                      (trade.profit > 0) ? 'text-emerald-400' : 'text-rose-400'
                    }`}>
                      {trade.profit ? (trade.profit > 0 ? `+${trade.profit}` : trade.profit) : '-'}
                    </td>

                    {/* 6. Status Badge */}
                    <td className="px-4 py-4 text-right">
                      <span className={`inline-block px-3 py-1 text-xs font-bold rounded-full border ${
                        isWin 
                          ? 'border-emerald-500/50 text-emerald-400 bg-emerald-500/10' 
                          : 'border-rose-500/50 text-rose-400 bg-rose-500/10'
                      }`}>
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
    </div>
  );
};

export default HistoryTable;