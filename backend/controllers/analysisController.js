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
const analyzeNewsImpact = async () => {
    try {
        const now = Math.floor(Date.now() / 1000);
        const twoHoursLater = now + 7200;

        // ----------------------------------------------------
        // 🚨 SIMULATION MODE: FAKE NEWS EVENT INJECTED
        // ----------------------------------------------------
        console.log("⚠️ SIMULATION: Injecting Fake CPI Event...");
        const upcomingNews = {
            event: 'CPI', // We use 'CPI' because we know it exists in history
            time: now + 1800, // Happening in 30 mins
            currency: 'USD',
            impact: 'High Impact Expected'
        };
        // ----------------------------------------------------

        console.log(`⚠️ Upcoming High Impact News Detected: ${upcomingNews.event}`);

        // 2. Search History (2018-2025)
        // Find all past instances of "CPI" to see how Gold reacted
        const pastEvents = await NewsEvent.find({
            event: { $regex: /CPI/i }, // Loose match for 'CPI'
            time: { $lt: now },
            currency: 'USD'
        })
        .sort({ time: -1 })
        .limit(50) 
        .select('time event actual forecast');

        console.log(`📜 Found ${pastEvents.length} past ${upcomingNews.event} events.`);

        if (pastEvents.length < 5) return null; 

        // 3. Correlate with Price (The Backtest)
        let upCount = 0;
        let downCount = 0;
        let validSamples = 0;

        for (const event of pastEvents) {
            // Find the 1H candle that started at the news time
            const candleTime = event.time - (event.time % 3600);
            const candle = await Candle.findOne({ time: candleTime, timeframe: '1h' });

            if (candle) {
                validSamples++;
                // Did price close higher than it opened?
                if (candle.close > candle.open) upCount++;
                else downCount++;
            }
        }

        console.log(`📊 Backtest Result: ${upCount} UP vs ${downCount} DOWN`);

        if (validSamples < 5) return null;

        // 4. Calculate Win Rate
        const winRateUp = (upCount / validSamples) * 100;
        const winRateDown = (downCount / validSamples) * 100;
        let signal = 'NEUTRAL';
        let confidence = 0;

        // 5. Generate Signal
        if (winRateUp >= 60) { // Lowered threshold slightly for test visibility
            signal = 'BUY';
            confidence = winRateUp;
        } else if (winRateDown >= 60) {
            signal = 'SELL';
            confidence = winRateDown;
        } else {
            signal = 'WAIT'; 
            confidence = 50; 
        }

        return {
            signal: signal,
            probability: confidence,
            reason: `Based on ${validSamples} past CPI events: Price dropped ${downCount} times and rose ${upCount} times.`,
            eventName: upcomingNews.event
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
        const newsStrategy = await analyzeNewsImpact();

        if (newsStrategy && newsStrategy.signal !== 'NEUTRAL') {
            return res.json({
                signal: newsStrategy.signal,
                confidence: newsStrategy.probability,
                pattern: 'Historical News Bias',
                trend: 'News Driven',
                reason: newsStrategy.reason,
                newsContext: `⚠️ Upcoming: ${newsStrategy.eventName}`
            });
        }

        // STEP 2: TECHNICAL ANALYSIS (Fallback)
        const rawCandles = await Candle.find({ timeframe: timeframe || '1h' })
                                    .sort({ time: -1 })
                                    .limit(60);
        
        if (rawCandles.length < 50) {
            return res.json({ signal: 'NEUTRAL', reason: 'Not enough data.' });
        }

        const candles = rawCandles.reverse(); 

        const analysis = detectPattern(candles);
        const sma50 = calculateSMA(candles, 50);
        const currentPrice = candles[candles.length - 1].close;
        const trend = currentPrice > sma50 ? 'UP' : 'DOWN';

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