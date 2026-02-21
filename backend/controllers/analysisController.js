const axios = require('axios'); // 🔌 Bridge to Python
const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');

// ⚙️ CONFIGURATION
const PYTHON_API_URL = process.env.PYTHON_API_URL || 'http://127.0.0.1:8000/api/analyze';

// ==========================================
// 🕵️ NEWS ANALYZER (UNCHANGED LEGACY)
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
// 🚀 MAIN CONTROLLER (MULTI-TIMEFRAME MATRIX)
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
                    reason: [newsStrategy.reason],
                    newsContext: `Stats: ${newsStrategy.stats.wins} Wins / ${newsStrategy.stats.losses} Losses`
                });
            }
        }

        // ------------------------------------
        // PATH B: SMC BRAIN (4H/1H Multi-Timeframe)
        // ------------------------------------
        
        // 1. Fetch Fresh Data from MongoDB for BOTH Timeframes simultaneously
        console.log("⏱️ Fetching 1H and 4H Data Matrix from MongoDB...");
        const [raw1HCandles, raw4HCandles] = await Promise.all([
             Candle.find({ timeframe: '1h' }).sort({ time: -1 }).limit(3000).lean(),
             Candle.find({ timeframe: '4h' }).sort({ time: -1 }).limit(1000).lean()
        ]);

        if (raw1HCandles.length < 100 || raw4HCandles.length < 50) {
            return res.json({ signal: 'NEUTRAL', confidence: 0, reason: ['Not enough data for Multi-Timeframe Matrix.'] });
        }

        // 2. Prepare Data for Python (Oldest -> Newest)
        const formatCandles = (candles) => candles.reverse().map(c => ({
            open: c.open, 
            high: c.high, 
            low: c.low, 
            close: c.close, 
            time: c.time, 
            volume: c.volume || 0
        }));

        const clean1HCandles = formatCandles(raw1HCandles);
        const clean4HCandles = formatCandles(raw4HCandles);
        const currentPrice = clean1HCandles[clean1HCandles.length - 1].close;

        // 3. FETCH RECENT FUNDAMENTAL NEWS
        const latestNews = await NewsEvent.findOne({ 
            impact: 'High', 
            actual: { $ne: null }, 
            forecast: { $ne: null } 
        }).sort({ time: -1 }).lean();

        // 4. Call the Python Brain with BOTH arrays
        console.log(`📡 Contacting SMC Brain: [1H: ${clean1HCandles.length}] + [4H: ${clean4HCandles.length}] + News...`);
        
        try {
            const pythonResponse = await axios.post(PYTHON_API_URL, {
                timeframe: timeframe || '1h', // Primary execution timeframe
                currency: currency || 'XAUUSD',
                current_price: currentPrice,
                candles: clean1HCandles,          // 👈 Sends the 1H Execution Data
                htf_candles: clean4HCandles,      // 👈 NEW: Sends the 4H Macro Data
                news_data: latestNews 
            });

            // 5. Return the Smart Response
            return res.json(pythonResponse.data);

        } catch (pyError) {
            console.error("⚠️ Python Brain Unreachable:", pyError.message);
            return res.json({
                signal: 'NEUTRAL', 
                confidence: 0,
                reason: ['SMC Engine is offline. Please check Python service.'], 
                error: pyError.message
            });
        }

    } catch (error) {
        console.error('❌ Analysis Error:', error);
        res.status(500).json({ error: 'Analysis Failed' });
    }
};