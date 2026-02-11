const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');
const TI = require('technicalindicators'); // 🧠 The New Brain Cell

// ==========================================
// 🕵️ NEWS ANALYZER (Keep this - it works!)
// ==========================================
const analyzeNewsImpact = async (targetEvent, targetCurrency) => {
    try {
        if (!targetEvent) return null;

        console.log(`🔎 Scouting History for: ${targetEvent} (${targetCurrency})...`);

        const pastEvents = await NewsEvent.find({
            event: targetEvent,
            currency: targetCurrency,
            actual: { $ne: null },
            forecast: { $ne: null }
        }).sort({ time: -1 }).limit(50);

        if (pastEvents.length < 3) return null;

        let logicalMoves = 0;
        let fakeouts = 0;
        let totalValid = 0;

        for (const news of pastEvents) {
            const deviation = news.actual - news.forecast;
            if (deviation === 0) continue;

            const candleTime = news.time - (news.time % 3600);
            const candle = await Candle.findOne({ time: candleTime, timeframe: '1h' });

            if (!candle) continue;

            totalValid++;
            const marketMove = candle.close - candle.open;
            const isMarketGreen = marketMove > 0;
            const isNewsPositive = deviation > 0;

            if ((isNewsPositive && isMarketGreen) || (!isNewsPositive && !isMarketGreen)) {
                logicalMoves++;
            } else {
                fakeouts++;
            }
        }

        if (totalValid === 0) return null;

        const winRate = (logicalMoves / totalValid) * 100;
        let signal = 'NEUTRAL';
        if (winRate > 65) signal = 'FOLLOW_NEWS';
        if (winRate < 35) signal = 'INVERSE_NEWS';

        return {
            signal: signal === 'FOLLOW_NEWS' ? 'HIGH RELIABILITY' : 'CAUTION',
            probability: Math.round(winRate),
            reason: `Analyzed ${totalValid} past events.\nMarket followed news logic ${Math.round(winRate)}% of the time.`,
            eventName: targetEvent,
            stats: { wins: logicalMoves, losses: fakeouts }
        };

    } catch (err) {
        console.error("❌ News Analysis Error:", err);
        return null;
    }
};

// ==========================================
// 🧠 PATTERN RECOGNITION ENGINE
// ==========================================
const detectAdvancedPatterns = (opens, highs, lows, closes) => {
    let signals = [];

    // 1. FOREX STRUCTURES (M & W Patterns)
    const doubleTop = TI.doubletop({ input: { highs, lows, close: closes, period: 20 } });
    const doubleBottom = TI.doublebottom({ input: { highs, lows, close: closes, period: 20 } });
    
    if (doubleTop) signals.push({ name: 'Double Top (M-Pattern)', type: 'BEARISH', strength: 85 });
    if (doubleBottom) signals.push({ name: 'Double Bottom (W-Pattern)', type: 'BULLISH', strength: 85 });

    // 2. HEAD AND SHOULDERS
    const hs = TI.headandshoulders({ input: { highs, lows, close: closes, period: 30 } });
    const invHs = TI.inverseheadandshoulders({ input: { highs, lows, close: closes, period: 30 } });
    
    if (hs) signals.push({ name: 'Head & Shoulders', type: 'BEARISH', strength: 90 });
    if (invHs) signals.push({ name: 'Inv. Head & Shoulders', type: 'BULLISH', strength: 90 });

    // 3. CANDLESTICK PATTERNS (The "Micro" View)
    // We analyze the last 5 candles for immediate signals
    const last5Open = opens.slice(-5);
    const last5High = highs.slice(-5);
    const last5Low = lows.slice(-5);
    const last5Close = closes.slice(-5);
    const input = { open: last5Open, high: last5High, low: last5Low, close: last5Close };

    if (TI.bullishengulfingpattern(input)) signals.push({ name: 'Bullish Engulfing', type: 'BULLISH', strength: 70 });
    if (TI.bearishengulfingpattern(input)) signals.push({ name: 'Bearish Engulfing', type: 'BEARISH', strength: 70 });
    if (TI.hammerpattern(input)) signals.push({ name: 'Hammer (Rejection)', type: 'BULLISH', strength: 65 });
    if (TI.shootingstarpattern(input)) signals.push({ name: 'Shooting Star', type: 'BEARISH', strength: 65 });

    return signals;
};

// ==========================================
// 🚀 MAIN CONTROLLER
// ==========================================
exports.analyzePattern = async (req, res) => {
    try {
        const { timeframe, eventName, currency } = req.body;

        // ------------------------------------
        // PATH A: NEWS EVENT ANALYSIS
        // ------------------------------------
        if (eventName) {
            const newsStrategy = await analyzeNewsImpact(eventName, currency || 'USD');
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
        }

        // ------------------------------------
        // PATH B: TECHNICAL ANALYSIS (Fallback)
        // ------------------------------------
        
        // 1. GET DATA (Need 100+ candles for indicators to work)
        const rawCandles = await Candle.find({ timeframe: timeframe || '1h' })
                                        .sort({ time: -1 })
                                        .limit(150);

        if (rawCandles.length < 50) {
            return res.json({ signal: 'NEUTRAL', confidence: 0, reason: 'Not enough data.' });
        }

        // 2. PREPARE DATA (Reverse to Oldest -> Newest)
        const candles = rawCandles.reverse();
        const opens = candles.map(c => c.open);
        const highs = candles.map(c => c.high);
        const lows = candles.map(c => c.low);
        const closes = candles.map(c => c.close);

        // 3. INDICATORS
        // Trend (EMA)
        const ema50 = TI.EMA.calculate({ period: 50, values: closes });
        const ema200 = TI.EMA.calculate({ period: 200, values: closes });
        
        const lastEma50 = ema50[ema50.length - 1];
        const lastEma200 = ema200[ema200.length - 1];
        let trend = lastEma50 > lastEma200 ? 'UPTREND' : 'DOWNTREND';

        // Momentum (RSI)
        const rsiArray = TI.RSI.calculate({ period: 14, values: closes });
        const lastRsi = rsiArray[rsiArray.length - 1];

        // 4. PATTERN RECOGNITION
        const patterns = detectAdvancedPatterns(opens, highs, lows, closes);

        // 5. CALCULATE SCORE
        let confidence = 0;
        let reasoning = [];
        let finalSignal = 'NEUTRAL';

        // A. Trend Score
        if (trend === 'UPTREND') {
            confidence += 30;
            reasoning.push("📈 Market Structure: Uptrend (EMA 50 > 200)");
        } else {
            confidence += 30;
            reasoning.push("📉 Market Structure: Downtrend (EMA 50 < 200)");
        }

        // B. RSI Score
        if (lastRsi > 70) {
            confidence -= 10; 
            reasoning.push("⚠️ RSI Overbought (>70). Risk of reversal.");
        } else if (lastRsi < 30) {
            confidence -= 10;
            reasoning.push("⚠️ RSI Oversold (<30). Risk of reversal.");
        } else {
            reasoning.push(`ℹ️ RSI is Neutral (${lastRsi.toFixed(1)})`);
        }

        // C. Pattern Score
        if (patterns.length > 0) {
            const bestPattern = patterns[patterns.length - 1]; // Most recent pattern
            reasoning.push(`✨ Pattern Detected: ${bestPattern.name}`);
            
            if (bestPattern.type === 'BULLISH') {
                if (trend === 'UPTREND') confidence += bestPattern.strength; // Pattern matches Trend
                else confidence += (bestPattern.strength / 2); // Counter-trend trade
                finalSignal = 'BUY';
            } else {
                if (trend === 'DOWNTREND') confidence += bestPattern.strength;
                else confidence += (bestPattern.strength / 2);
                finalSignal = 'SELL';
            }
        } else {
            reasoning.push("No clear chart patterns detected.");
        }

        // Cap Confidence
        confidence = Math.min(98, Math.max(0, confidence));
        if (confidence < 45) finalSignal = 'NEUTRAL';

        res.json({
            signal: finalSignal,
            confidence: Math.round(confidence),
            trend: trend,
            reason: reasoning.join('\n'), // Send as multi-line text
            pattern: patterns.length > 0 ? patterns[patterns.length - 1].name : 'None'
        });

    } catch (error) {
        console.error('❌ Analysis Error:', error);
        res.status(500).json({ error: 'Analysis Failed' });
    }
};