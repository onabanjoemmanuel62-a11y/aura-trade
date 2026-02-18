const axios = require('axios'); // 🔌 Bridge to Python
const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');

// ⚙️ CONFIGURATION
// If you are on Render, this URL might need to be your internal Render URL.
// For local dev, it is usually port 8000.
const PYTHON_API_URL = process.env.PYTHON_API_URL || 'http://127.0.0.1:8000/api/analyze';

// ==========================================
// 🕵️ NEWS ANALYZER (UNCHANGED - PRESERVED)
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
// 🚀 MAIN CONTROLLER
// ==========================================
exports.analyzePattern = async (req, res) => {
    try {
        const { timeframe, eventName, currency } = req.body;

        // ------------------------------------
        // PATH A: NEWS EVENT ANALYSIS (Legacy Logic)
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
        // PATH B: SMC BRAIN (The New Python Bridge)
        // ------------------------------------
        
        // 1. Fetch Fresh Data from MongoDB
        // We need enough candles for the Python Fractal scan (min 100+)
        const rawCandles = await Candle.find({ timeframe: timeframe || '1h' })
                                       .sort({ time: -1 }) // Get newest first
                                       .limit(300)         // Get plenty of history
                                       .lean();            // Convert to plain JSON

        if (rawCandles.length < 100) {
            return res.json({ signal: 'NEUTRAL', confidence: 0, reason: 'Not enough data for SMC analysis.' });
        }

        // 2. Prepare Data for Python (Oldest -> Newest)
        // Python expects a clean array of objects
        const cleanCandles = rawCandles.reverse().map(c => ({
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
            time: c.time, // Ensure this matches what brain.py expects
            volume: c.volume || 0
        }));

        const currentPrice = cleanCandles[cleanCandles.length - 1].close;

        // 3. Call the Python Brain
        console.log(`📡 Contacting SMC Brain for ${cleanCandles.length} candles...`);
        
        try {
            const pythonResponse = await axios.post(PYTHON_API_URL, {
                timeframe: timeframe || '1h',
                currency: currency || 'XAUUSD',
                current_price: currentPrice,
                candles: cleanCandles // 👈 SENDING LIVE DATA
            });

            // 4. Return the Smart Response
            return res.json(pythonResponse.data);

        } catch (pyError) {
            console.error("⚠️ Python Brain Unreachable:", pyError.message);
            // Fallback if Python is offline
            return res.json({
                signal: 'NEUTRAL',
                confidence: 0,
                reason: 'SMC Engine is offline. Please check Python service.',
                error: pyError.message
            });
        }

    } catch (error) {
        console.error('❌ Analysis Error:', error);
        res.status(500).json({ error: 'Analysis Failed' });
    }
};