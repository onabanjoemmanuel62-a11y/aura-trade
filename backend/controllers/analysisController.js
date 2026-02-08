const similarity = require('compute-cosine-similarity');
const Candle = require('../models/Candle');

// @desc    Scan history for High-Precision "Twin" patterns
// @route   POST /api/analyze/pattern
// @access  Public
const analyzePattern = async (req, res) => {
  try {
    const { timeframe, currentPattern } = req.body; 

    // 1. Validation
    if (!currentPattern || currentPattern.length < 5) {
      return res.status(400).json({ message: 'Pattern array is too short.' });
    }

    // 2. Fetch History (Optimized)
    // We assume you are now sending '1m' or '1h' as the timeframe
    const history = await Candle.find({ timeframe: timeframe || '1h' }) 
      .sort({ time: 1 }) // Oldest -> Newest
      .select('close open time'); 

    if (history.length < 100) {
      return res.status(400).json({ message: 'Not enough historical data to scan.' });
    }

    const patternLength = currentPattern.length;
    const threshold = 0.90; // Minimum entry requirement (we will filter stricter later)
    let potentialMatches = [];

    console.log(`🔍 Scanning ${history.length} candles for precision matches...`);

    // 3. The Sliding Window Scan
    for (let i = 0; i < history.length - patternLength - 1; i++) {
      
      const historicalSlice = history.slice(i, i + patternLength).map(c => c.close);

      // Skip if lengths don't match exactly due to data gaps
      if (historicalSlice.length !== patternLength) continue;

      // Calculate Similarity
      const sim = similarity(currentPattern, historicalSlice);

      // 4. Collect Candidates
      if (sim >= threshold) {
        // Look at the "Future" candle (the one immediately after the pattern)
        const nextCandle = history[i + patternLength];
        const isUp = nextCandle.close > nextCandle.open;

        potentialMatches.push({
          similarity: sim,
          nextCandle: nextCandle,
          result: isUp ? 'UP' : 'DOWN'
        });
      }
    }

    // 5. The Sniper Filter: Sort & Limit
    // Sort by Similarity (Highest first)
    potentialMatches.sort((a, b) => b.similarity - a.similarity);

    // Keep ONLY the Top 50 "Perfect Twins"
    // If we have less than 50, we take them all.
    const bestMatches = potentialMatches.slice(0, 50);

    // 6. Calculate Stats on the Elite Set
    let upCount = 0;
    let downCount = 0;

    bestMatches.forEach(match => {
      if (match.result === 'UP') upCount++;
      else downCount++;
    });

    const totalElite = bestMatches.length;
    let probabilityUp = 0;
    let probabilityDown = 0;
    let sentiment = 'NEUTRAL';

    if (totalElite > 0) {
      probabilityUp = ((upCount / totalElite) * 100).toFixed(1);
      probabilityDown = ((downCount / totalElite) * 100).toFixed(1);

      // Define Sentiment
      if (Number(probabilityUp) > 55) sentiment = 'BULLISH';
      else if (Number(probabilityDown) > 55) sentiment = 'BEARISH';
    }

    // 7. Return Precision Data
    res.json({
      matchesFound: totalElite, // Now shows e.g., "50" instead of "124,000"
      totalScanned: potentialMatches.length, // Optional: Shows how many raw matches existed
      probabilityUp: parseFloat(probabilityUp),
      probabilityDown: parseFloat(probabilityDown),
      sentiment
    });

  } catch (error) {
    console.error("Analysis Error:", error);
    res.status(500).json({ message: 'Server Error during analysis' });
  }
};

module.exports = { analyzePattern };