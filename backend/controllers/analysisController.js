const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');
const fs = require('fs');
const path = require('path');

// CONFIGURATION
const PATTERN_LENGTH = 8;        
const FORECAST_HORIZON = 4;      
const NEWS_FILE = path.join(__dirname, '../data/news_cache.json');

// ==========================================
// 🧠 HELPER: CALCULATE SMA (Trend)
// ==========================================
const calculateSMA = (candles, period) => {
    if (candles.length < period) return null;
    const slice = candles.slice(candles.length - period);
    const sum = slice.reduce((acc, c) => acc + c.close, 0);
    return sum / period;
};

// ==========================================
// 🧠 HELPER: DETECT PATTERNS
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

    return { type: 'No Clear Pattern', bias: 'NEUTRAL', strength: 0 };
};

// ==========================================
// 🕵️ NEW ENGINE: NEWS IMPACT ANALYZER (70% STRATEGY)
// ==========================================
// Checks history to see how price reacted to this specific event name in the past.
const analyzeNewsImpact = async () => {
    try {
        const now = Math.floor(Date.now() / 1000);
        const twoHoursLater = now + 7200;

        // 1. Identify Upcoming News (Next 2 Hours)
        const upcomingNews = await NewsEvent.findOne({
            time: { $gt: now, $lt: twoHoursLater },
            currency: 'USD',
            impact: { $regex: /High/i } // 'High Impact Expected' or 'High'
        }).sort({ time: 1 });

        if (!upcomingNews) return null; // No news, continue with technicals

        console.log(`⚠️ Upcoming High Impact News Detected: ${upcomingNews.event}`);

        // 2. Search History (2018-2025)
        // Find all past instances of "CPI", "NFP", etc.
        const pastEvents = await NewsEvent.find({
            event: upcomingNews.event,
            time: { $lt: now },
            currency: 'USD'
        })
        .sort({ time: -1 })
        .limit(50) // Analyze last 50 occurrences
        .select('time event actual forecast');

        if (pastEvents.length < 5) return null; // Not enough data to be sure

        // 3. Correlate with Price (The Backtest)
        let upCount = 0;
        let downCount = 0;
        let validSamples = 0;

        for (const event of pastEvents) {
            // Find the 1H candle that started at (or around) the news time
            // We round down to the nearest hour to match candle timestamps
            const candleTime = event.time - (event.time % 3600);

            const candle = await Candle.findOne({ time: candleTime, timeframe: '1h' });

            if (candle) {
                validSamples++;
                // Did price close higher than it opened during the news hour?
                if (candle.close > candle.open) upCount++;
                else downCount++;
            }
        }

        if (validSamples < 5) return null;

        // 4. Calculate Win Rate
        const winRateUp = (upCount / validSamples) * 100;
        const winRateDown = (downCount / validSamples) * 100;
        let signal = 'NEUTRAL';
        let confidence = 0;

        // 5. Generate Signal (Only if > 70%)
        if (winRateUp >= 70) {
            signal = 'BUY';
            confidence = winRateUp;
        } else if (winRateDown >= 70) {
            signal = 'SELL';
            confidence = winRateDown;
        } else {
            // If historical reaction is mixed (e.g. 50/50), it's dangerous -> WAIT
            signal = 'WAIT'; 
            confidence = 50; 
        }

        return {
            type: 'HISTORICAL_NEWS_BIAS',
            eventName: upcomingNews.event,
            signal: signal,
            probability: confidence,
            reason: `In ${validSamples} past '${upcomingNews.event}' events, Price dropped ${downCount} times and rose ${upCount} times.`,
            isOverride: true // Tells the controller to ignore technicals
        };

    } catch (err) {
        console.error("News Impact Analysis Error:", err);
        return null;
    }
};

// ==========================================
// 🚀 MAIN CONTROLLER
// ==========================================
exports.analyzePattern = async (req, res) => {
    try {
        const { currentPattern, timeframe } = req.body;

        // STEP 1: RUN NEWS IMPACT ANALYZER FIRST
        // If there is a clear historical bias for upcoming news, we execute that strategy.
        const newsStrategy = await analyzeNewsImpact();

        if (newsStrategy && newsStrategy.signal !== 'NEUTRAL' && newsStrategy.signal !== 'WAIT') {
            return res.json({
                signal: newsStrategy.signal,
                confidence: newsStrategy.probability,
                pattern: 'News Event Bias',
                trend: 'News Driven',
                reason: newsStrategy.reason,
                newsContext: `⚠️ Upcoming: ${newsStrategy.eventName}`
            });
        }
        
        // If News Strategy says WAIT (mixed history), we pause trading.
        if (newsStrategy && newsStrategy.signal === 'WAIT') {
            return res.json({
                signal: 'NEUTRAL',
                confidence: 0,
                pattern: 'News Uncertainty',
                trend: 'Volatile',
                reason: newsStrategy.reason + " (Market is unpredictable during this event)",
                newsContext: `⚠️ Upcoming: ${newsStrategy.eventName}`
            });
        }

        // STEP 2: IF NO NEWS, RUN TECHNICAL ANALYSIS
        const rawCandles = await Candle.find({ timeframe: timeframe || '1h' })
                                    .sort({ time: -1 })
                                    .limit(60);
        
        if (rawCandles.length < 50) {
            return res.json({ signal: 'NEUTRAL', reason: 'Not enough data.' });
        }

        const candles = rawCandles.reverse(); // Oldest -> Newest

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

        // Final Technical Response
        res.json({
            signal: analysis.bias,
            confidence: analysis.strength,
            pattern: analysis.type,
            trend: trend,
            reason: analysis.reason,
            newsContext: "No major upcoming news."
        });

    } catch (error) {
        console.error('❌ Analysis Error:', error);
        res.status(500).json({ error: 'Analysis Failed' });
    }
};