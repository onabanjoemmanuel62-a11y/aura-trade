const express = require('express');
const router = express.Router();
const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');
const { calculateNewsBias } = require('../utils/newsLogic');

// @desc    Analyze market and generate a trade signal
// @route   POST /api/analyze
router.post('/', async (req, res) => {
  try {
    const { time, currentPrice } = req.body;

    if (!time || !currentPrice) {
      return res.status(400).json({ message: "Please provide 'time' and 'currentPrice'" });
    }

    // 1. Initialize strictly as an Array
    let reasoning = [];
    let patternSignal = 'NEUTRAL';
    let newsSignal = 'NEUTRAL';

    // ==========================================
    // 🧠 1. PATTERN RECOGNITION (The Technicals)
    // ==========================================
    const recentCandles = await Candle.find({ 
      time: { $lte: time }, 
      timeframe: '1h' 
    })
    .sort({ time: -1 })
    .limit(3);

    if (recentCandles.length === 3) {
      const [c1, c2, c3] = recentCandles;

      // Bearish Pattern
      if (c1.close < c1.open && c2.close < c2.open && c3.close < c3.open) {
        patternSignal = 'BEARISH';
        reasoning.push("Technical: Three Black Crows pattern detected");
      }
      // Bullish Pattern
      else if (c1.close > c1.open && c2.close > c2.open && c3.close > c3.open) {
        patternSignal = 'BULLISH';
        reasoning.push("Technical: Three White Soldiers pattern detected");
      }
    }

    // ==========================================
    // 📰 2. SENTIMENT ANALYSIS (The Fundamentals)
    // ==========================================
    const oneDayAgo = time - (24 * 60 * 60);
    
    // Sort Newest First so we get the latest news
    const recentNews = await NewsEvent.find({
      time: { $gte: oneDayAgo, $lte: time },
      currency: 'USD',
      impact: 'High Impact Expected'
    }).sort({ time: -1 });

    // ... inside your route ...
if (recentNews.length > 0) {
  const lastEvent = recentNews[0];

  // ONE LINE TO RULE THEM ALL:
  const newsAction = calculateNewsBias(lastEvent, 'XAUUSD'); // Returns 'SELL', 'BUY', or 'NEUTRAL'

  if (newsAction === 'SELL') {
    newsSignal = 'BEARISH';
    reasoning.push(`Fundamental: Strong USD News (${lastEvent.event}: ${lastEvent.actual} > ${lastEvent.forecast})`);
  } else if (newsAction === 'BUY') {
    newsSignal = 'BULLISH';
    reasoning.push(`Fundamental: Weak USD News (${lastEvent.event}: ${lastEvent.actual} < ${lastEvent.forecast})`);
  }
}

    // ==========================================
    // ⚖️ 3. THE DECISION ENGINE (The Brain)
    // ==========================================
    let finalSignal = 'WAIT'; // Default
    let confidence = 50;      // Default

    // Logic Matrix
    if (newsSignal === 'BEARISH' && patternSignal === 'BEARISH') {
      finalSignal = 'STRONG SELL';
      confidence = 87;
    } 
    else if (newsSignal === 'BULLISH' && patternSignal === 'BULLISH') {
      finalSignal = 'STRONG BUY';
      confidence = 92;
    }
    else if (newsSignal === 'BEARISH') {
      finalSignal = 'SELL';
      confidence = 65;
    }
    else if (newsSignal === 'BULLISH') {
      finalSignal = 'BUY';
      confidence = 65;
    }

    // SAFETY CHECK: If no reasons exist, add a default message
    // This prevents the frontend from crashing or showing an empty box
    if (reasoning.length === 0) {
      reasoning.push("Market is choppy. No clear patterns or recent news.");
    }

    // Calculate Targets
    let entry = currentPrice;
    let stopLoss = 0;
    let takeProfit = 0;

    if (finalSignal.includes('BUY')) {
      stopLoss = currentPrice * 0.995; 
      takeProfit = currentPrice * 1.015; 
    } else if (finalSignal.includes('SELL')) {
      stopLoss = currentPrice * 1.005;
      takeProfit = currentPrice * 0.985;
    }

    res.json({
      signal: finalSignal,
      confidence,
      entry,
      stopLoss: parseFloat(stopLoss.toFixed(2)),
      takeProfit: parseFloat(takeProfit.toFixed(2)),
      reasoning // This is GUARANTEED to be an Array of Strings now
    });

  } catch (error) {
    console.error("Analysis Error:", error);
    res.status(500).json({ message: "Analysis Failed" });
  }
});

module.exports = router;