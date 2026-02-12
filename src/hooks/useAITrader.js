import { useState, useCallback } from 'react';
import axios from 'axios';

export const useAITrader = () => {
  const [aiSignal, setSignal] = useState(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);

  // This function takes the candles from your chart and asks the AI for an opinion
  const analyzeMarket = useCallback(async (candles) => {
    if (!candles || candles.length < 50) return; // AI needs context (at least 50 candles)

    setIsAnalyzing(true);
    try {
      // 🚀 SEND DATA TO YOUR RENDER BACKEND
      // We send the last 100 candles to optimize payload size
      const recentCandles = candles.slice(-100);
      
      const response = await axios.post('https://aura-trade.onrender.com/api/analyze', {
        candles: recentCandles,
        pair: 'XAUUSD', // You can make this dynamic later
        timeframe: '1H'
      });

      // 🧠 UPDATE SIGNAL STATE WITH REAL AI RESPONSE
      setSignal({
        type: response.data.signal,        // "BUY" or "SELL" or "HOLD"
        confidence: response.data.confidence, // e.g., 96
        reason: response.data.pattern,     // e.g., "Bullish Flag + RSI Divergence"
        levels: response.data.keyLevels,   // Support/Resistance numbers
        timestamp: new Date()
      });

    } catch (error) {
      console.error("AI Brain Freeze:", error);
    } finally {
      setIsAnalyzing(false);
    }
  }, []);

  return { aiSignal, isAnalyzing, analyzeMarket };
};