const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');
const fs = require('fs');
const path = require('path');

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
// 🕵️ REAL ENGINE: NEWS IMPACT ANALYZER (PHASE 2)
// ==========================================
const analyzeNewsImpact = async (targetEvent, targetCurrency) => {
    try {
        if (!targetEvent) return null; // No event selected by user

        console.log(`🔎 Scouting History for: ${targetEvent} (${targetCurrency})...`);

        // 1. Search History (Real Database Query)
        // Find past instances where we have actual data AND a forecast
        const pastEvents = await NewsEvent.find({
            event: targetEvent,
            currency: targetCurrency,
            actual: { $ne: null },
            forecast: { $ne: null }
        })
        .sort({ time: -1 }) // Newest first
        .limit(50); // Analyze last 50 matches

        if (pastEvents.length < 3) {
            console.log("⚠️ Not enough historical data for this event.");
            return null;
        }

        let logicalMoves = 0; // Times the market obeyed the news
        let fakeouts = 0;     // Times the market did the opposite
        let totalValid = 0;

        // 2. The Replay Loop
        for (const news of pastEvents) {
            
            // Calculate Deviation (Did news surprise the market?)
            // If Actual > Forecast, Deviation is Positive
            const deviation = news.actual - news.forecast;
            
            if (deviation === 0) continue; // Skip events with no surprise

            // Find the 1H candle that started at or near the news time
            // We align to the start of the hour to match candle timestamps
            const candleTime = news.time - (news.time % 3600); 
            
            const candle = await Candle.findOne({ time: candleTime, timeframe: '1h' });

            if (!candle) continue; // No candle data for that specific day

            totalValid++;

            const marketMove = candle.close - candle.open;
            const isMarketGreen = marketMove > 0;
            const isNewsPositive = deviation > 0;

            // 3. The VAR Check (Win/Loss Logic)
            // Rule: Positive News should mean Green Candle (Standard Logic)
            // Note: For XAUUSD, this might need inversion later.
            
            if ((isNewsPositive && isMarketGreen) || (!isNewsPositive && !isMarketGreen)) {
                logicalMoves++; // WIN: Market moved in direction of news
            } else {
                fakeouts++;     // LOSS: Market ignored news
            }
        }

        if (totalValid === 0) return null;

        // 4. Calculate Real Win Rate
        const winRate = (logicalMoves / totalValid) * 100;
        
        let signal = 'NEUTRAL';
        if (winRate > 65) signal = 'FOLLOW_NEWS'; // High reliability
        if (winRate < 35) signal = 'INVERSE_NEWS'; // Market usually does the opposite!

        return {
            signal: signal === 'FOLLOW_NEWS' ? 'HIGH RELIABILITY' : 'CAUTION',
            probability: Math.round(winRate),
            reason: `Analyzed ${totalValid} past events. The market followed the news logic ${Math.round(winRate)}% of the time.`,
            eventName: targetEvent,
            stats: { wins: logicalMoves, losses: fakeouts }
        };

    } catch (err) {
        console.error("❌ News Analysis Error:", err);
        return null;
    }
};

// ==========================================
// 🚀 MAIN CONTROLLER
// ==========================================
exports.analyzePattern = async (req, res) => {
    try {
        // Now accepting eventName and currency from Frontend
        const { currentPattern, timeframe, eventName, currency } = req.body;

        // ----------------------------------------------------
        // STEP 1: RUN REAL HISTORICAL ANALYSIS (If Event Selected)
        // ----------------------------------------------------
        let newsStrategy = null;
        if (eventName) {
            newsStrategy = await analyzeNewsImpact(eventName, currency || 'USD');
        }

        // If we found a strong news correlation, return that immediately
        if (newsStrategy && newsStrategy.probability > 60) {
            return res.json({
                signal: newsStrategy.probability > 60 ? 'STRONG CORRELATION' : 'WEAK',
                confidence: newsStrategy.probability,
                pattern: 'Historical News Bias',
                trend: 'News Driven',
                reason: newsStrategy.reason,
                newsContext: `Stats: ${newsStrategy.stats.wins} Wins / ${newsStrategy.stats.losses} Losses`
            });
        }

        // ----------------------------------------------------
        // STEP 2: TECHNICAL ANALYSIS (The Fallback)
        // ----------------------------------------------------
        // This runs if there is no news event OR if the news correlation is weak
        
        const rawCandles = await Candle.find({ timeframe: timeframe || '1h' })
                                        .sort({ time: -1 })
                                        .limit(60);
        
        if (rawCandles.length < 50) {
            return res.json({ signal: 'NEUTRAL', reason: 'Not enough candle data.' });
        }

        const candles = rawCandles.reverse(); // Need oldest -> newest for calcs

        const analysis = detectPattern(candles);
        const sma50 = calculateSMA(candles, 50);
        const currentPrice = candles[candles.length - 1].close;
        const trend = currentPrice > sma50 ? 'UP' : 'DOWN';

        // Boost score if Pattern matches Trend
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
            analysis.reason = "Market is ranging. No clear technical setup.";
        }

        res.json({
            signal: analysis.bias,
            confidence: analysis.strength,
            pattern: analysis.type,
            trend: trend,
            reason: analysis.reason,
            newsContext: "Technical Analysis Only (No News Event Selected)"
        });

    } catch (error) {
        console.error('❌ Analysis Error:', error);
        res.status(500).json({ error: 'Analysis Failed' });
    }
};