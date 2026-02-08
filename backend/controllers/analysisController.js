const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');
const fs = require('fs');
const path = require('path');

// CONFIGURATION
const PATTERN_LENGTH = 8;        
const FORECAST_HORIZON = 4;      
const SIMILARITY_THRESHOLD = 5.0; 
const NEWS_FILE = path.join(__dirname, '../data/news_cache.json');

// ==========================================
// 🧠 HELPER 1: CALCULATE TREND (SMA 50)
// ==========================================
const calculateSMA = (candles, period) => {
    if (candles.length < period) return null;
    const slice = candles.slice(candles.length - period);
    const sum = slice.reduce((acc, c) => acc + c.close, 0);
    return sum / period;
};

// ==========================================
// 🧠 HELPER 2: DETECT CANDLESTICK PATTERNS
// ==========================================
const detectPattern = (candles) => {
    const last = candles[candles.length - 1];       
    const prev = candles[candles.length - 2];       

    // 1. Bullish Engulfing
    if (prev.close < prev.open && last.close > last.open && 
        last.close > prev.open && last.open < prev.close) {
        return { type: 'Bullish Engulfing', bias: 'BUY', strength: 80 };
    }

    // 2. Bearish Engulfing
    if (prev.close > prev.open && last.close < last.open && 
        last.close < prev.open && last.open > prev.close) {
        return { type: 'Bearish Engulfing', bias: 'SELL', strength: 80 };
    }

    // 3. Hammer
    const body = Math.abs(last.close - last.open);
    const wick = last.high - Math.max(last.close, last.open);
    const tail = Math.min(last.close, last.open) - last.low;
    
    if (tail > body * 2 && wick < body * 0.5) {
        return { type: 'Hammer / Pinbar', bias: 'BUY', strength: 75 };
    }

    // 4. Shooting Star
    if (wick > body * 2 && tail < body * 0.5) {
        return { type: 'Shooting Star', bias: 'SELL', strength: 75 };
    }

    return { type: 'No Clear Pattern', bias: 'NEUTRAL', strength: 0 };
};

// ==========================================
// 🧠 HELPER 3: FRACTAL MATH (Vector Norm)
// ==========================================
const normalizePattern = (prices) => {
  if (!prices || prices.length === 0) return [];
  const basePrice = prices[0];
  return prices.map(p => ((p - basePrice) / basePrice) * 100);
};

const calculateDistance = (patternA, patternB) => {
  if (patternA.length !== patternB.length) return Infinity;
  let sum = 0;
  for (let i = 0; i < patternA.length; i++) {
    sum += Math.pow(patternA[i] - patternB[i], 2);
  }
  return Math.sqrt(sum);
};

// ==========================================
// 🚀 MAIN CONTROLLER
// ==========================================
exports.analyzePattern = async (req, res) => {
    try {
        const { currentPattern, timeframe } = req.body;

        // 1. FETCH DATA (Need 60 candles for SMA)
        const rawCandles = await Candle.find({ timeframe: timeframe || '1h' })
                                    .sort({ time: -1 })
                                    .limit(60);
        
        if (rawCandles.length < 50) {
            return res.json({ sentiment: 'NEUTRAL', reason: 'Not enough data for SMA.' });
        }

        // Correct Order: Oldest -> Newest
        const candles = rawCandles.reverse(); 

        // -----------------------------------------
        // A. TECHNICAL ANALYSIS LAYER
        // -----------------------------------------
        const analysis = detectPattern(candles);
        const sma50 = calculateSMA(candles, 50);
        const currentPrice = candles[candles.length - 1].close;
        const trend = currentPrice > sma50 ? 'UP' : 'DOWN';

        // Trend Filter Logic
        if (analysis.bias === 'BUY' && trend === 'UP') {
            analysis.strength += 10; 
            analysis.reason = `Strong ${analysis.type} in Uptrend`;
        } else if (analysis.bias === 'SELL' && trend === 'DOWN') {
            analysis.strength += 10;
            analysis.reason = `Strong ${analysis.type} in Downtrend`;
        } else if (analysis.bias !== 'NEUTRAL') {
            analysis.strength -= 20; 
            analysis.reason = `Weak ${analysis.type} (Counter-trend)`;
        } else {
            analysis.reason = "Market is ranging. No technical setup.";
        }

        // -----------------------------------------
        // B. FRACTAL ANALYSIS LAYER (The Time Machine)
        // -----------------------------------------
        let fractalSentiment = 'NEUTRAL';
        let matches = [];
        
        // Only run fractal search if we have a pattern from frontend
        if (currentPattern && currentPattern.length >= 5) {
             // ... (Fractal Logic Here - kept lightweight for speed)
             // For now, we will assume the frontend sends the pattern to scan
             // Or we can use the 'candles' we just fetched from DB
             const recentPrices = candles.slice(-8).map(c => c.close); // Last 8 candles
             const targetVector = normalizePattern(recentPrices);

             // Quick scan of the 60 loaded candles (or load more if needed)
             // In a real app, you'd scan the whole DB here, but let's keep it fast
             // We return "Not Scanned" for now to save processing power on this route
             fractalSentiment = 'UNSCANNED';
        }

        // -----------------------------------------
        // C. NEWS SAFETY SWITCH (The Red Button)
        // -----------------------------------------
        // 1. Check File Cache first (Fastest)
        let dangerNews = null;
        if (fs.existsSync(NEWS_FILE)) {
            const newsData = JSON.parse(fs.readFileSync(NEWS_FILE, 'utf8'));
            const now = Date.now();
            dangerNews = newsData.find(n => {
                const eventTime = new Date(n.time * 1000).getTime(); // Ensure seconds to ms
                return eventTime > now && eventTime < (now + 7200000); // Next 2 hours
            });
        }
        
        // 2. Check DB if File Cache misses (Backup)
        if (!dangerNews) {
            const nowSec = Math.floor(Date.now() / 1000);
            const dbNews = await NewsEvent.findOne({
                time: { $gte: nowSec, $lte: nowSec + 7200 },
                impact: { $regex: /High/i },
                currency: 'USD'
            });
            if (dbNews) dangerNews = { event: dbNews.event, currency: dbNews.currency };
        }

        // 3. EXECUTE KILL SWITCH
        if (dangerNews) {
            analysis.strength = 0;
            analysis.bias = 'NEUTRAL';
            analysis.reason = `⚠️ HALT: High Impact News Incoming (${dangerNews.event || dangerNews.title})`;
        }

        // -----------------------------------------
        // D. FINAL RESPONSE
        // -----------------------------------------
        res.json({
            signal: analysis.bias,
            confidence: analysis.strength, // 0 - 100
            pattern: analysis.type,
            trend: trend,
            reason: analysis.reason,
            // Keep the fractal data structure so frontend doesn't break
            probabilityUp: 0, 
            probabilityDown: 0,
            matchesFound: 0 
        });

    } catch (error) {
        console.error('❌ Analysis Error:', error);
        res.status(500).json({ error: 'Analysis Failed' });
    }
};