const express = require('express');
const router = express.Router();
const Candle = require('../models/Candle');

// ==========================================
// 🧠 HELPER: COSINE SIMILARITY (The "Eye")
// ==========================================
// Compares two arrays of numbers to see how similar they look (0 to 1)
const calculateSimilarity = (patternA, patternB) => {
    let dotProduct = 0;
    let normA = 0;
    let normB = 0;
    for (let i = 0; i < patternA.length; i++) {
        dotProduct += patternA[i] * patternB[i];
        normA += patternA[i] ** 2;
        normB += patternB[i] ** 2;
    }
    return dotProduct / (Math.sqrt(normA) * Math.sqrt(normB));
};

// ==========================================
// 📉 HELPER: ZIGZAG TRENDLINES (The "Pen")
// ==========================================
const findTrendlines = (candles) => {
    let swings = [];
    // Simple Swing Detection (Pivot Points)
    for(let i=2; i<candles.length-2; i++) {
        const c = candles[i];
        const prev = candles[i-1];
        const next = candles[i+1];

        // Swing High
        if(c.high > prev.high && c.high > next.high) {
            swings.push({ time: c.time, price: c.high, type: 'RESISTANCE' });
        }
        // Swing Low
        else if(c.low < prev.low && c.low < next.low) {
            swings.push({ time: c.time, price: c.low, type: 'SUPPORT' });
        }
    }
    return swings.slice(-4); // Return last 4 key levels
};

// ==========================================
// 🧠 CORE LOGIC: THE ANALYZER
// ==========================================
// We export this function so server.js can use it automatically
const analyzeMarket = async (time, timeframe) => {
    try {
        // 1. GET LIVE PATTERN (Last 50 Candles)
        const liveCandles = await Candle.find({ timeframe }).sort({ time: -1 }).limit(50);
        
        if (liveCandles.length < 50) return { signal: "WAIT", reasoning: ["Not enough data to analyze"] };

        // Normalize Live Data (Scale 0 to 1)
        const closes = liveCandles.map(c => c.close).reverse();
        const min = Math.min(...closes);
        const max = Math.max(...closes);
        const normalizedLive = closes.map(p => (p - min) / (max - min));

        // 2. SCAN HISTORY (The "Time Travel")
        // ⚠️ OPTIMIZATION: We scan a random sample of 2000 candles to keep the server fast.
        const historySample = await Candle.aggregate([
            { $match: { timeframe: timeframe, time: { $lt: time - 86400 } } }, // Older than 24h
            { $sample: { size: 2000 } }, 
            { $sort: { time: 1 } }
        ]);

        let bestMatch = null;
        let bestScore = -1;

        // Sliding Window Search
        for (let i = 0; i < historySample.length - 70; i++) {
            const segment = historySample.slice(i, i + 50);
            const segmentCloses = segment.map(c => c.close);
            
            // Normalize Segment
            const sMin = Math.min(...segmentCloses);
            const sMax = Math.max(...segmentCloses);
            const normalizedSegment = segmentCloses.map(p => (p - sMin) / (sMax - sMin));

            // Compare
            const score = calculateSimilarity(normalizedLive, normalizedSegment);

            if (score > bestScore) {
                bestScore = score;
                bestMatch = { segment, index: i, time: segment[0].time };
            }
        }

        // 3. EXTRACT GHOST PATH (The Future)
        let ghostPath = [];
        let signal = 'WAIT';
        let reasoning = [];
        let nextMovePrediction = 0;

        if (bestMatch && bestScore > 0.80) {
            // Grab the 20 candles that happened AFTER the match
            const futureSegment = historySample.slice(bestMatch.index + 50, bestMatch.index + 70);
            ghostPath = futureSegment.map(c => c.close);

            // Calculate Return
            const entry = bestMatch.segment[49].close;
            const exit = futureSegment[futureSegment.length - 1].close;
            nextMovePrediction = (exit - entry) / entry;

            reasoning.push(`Found ${(bestScore*100).toFixed(0)}% Match in History (${new Date(bestMatch.time * 1000).getFullYear()})`);

            if (nextMovePrediction > 0.001) {
                signal = 'BUY';
                reasoning.push("Historical Pattern resolved BULLISH");
            } else if (nextMovePrediction < -0.001) {
                signal = 'SELL';
                reasoning.push("Historical Pattern resolved BEARISH");
            }
        } else {
            reasoning.push("Market is in a Unique Phase (No historical match found)");
        }

        // 4. TRENDLINES
        const keyLevels = findTrendlines(liveCandles);
        reasoning.push(`Key Levels: ${keyLevels.map(l => l.price).join(', ')}`);

        return {
            signal,
            confidence: Math.round(bestScore * 100),
            reasoning,
            ghostPath // <--- THE DATA FOR THE PURPLE LINE
        };

    } catch (error) {
        console.error("AI Error:", error);
        return { signal: "ERROR", reasoning: ["AI Brain Malfunction"] };
    }
};

// @route POST /api/analyze (Manual Trigger)
router.post('/', async (req, res) => {
    const result = await analyzeMarket(req.body.time, req.body.timeframe || '1h');
    res.json(result);
});

module.exports = { router, analyzeMarket };