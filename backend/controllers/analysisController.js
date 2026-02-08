const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');

// CONFIGURATION
const PATTERN_LENGTH = 8;        // Input size
const FORECAST_HORIZON = 4;      // Look ahead size
const SIMILARITY_THRESHOLD = 5.0; // Euclidean Distance Threshold

// ==========================================
// 🧮 MATH ENGINE: Vector Normalization
// ==========================================
const normalizePattern = (prices) => {
  if (!prices || prices.length === 0) return [];
  const basePrice = prices[0];
  return prices.map(p => ((p - basePrice) / basePrice) * 100);
};

// ==========================================
// 🧮 MATH ENGINE: Euclidean Distance
// ==========================================
const calculateDistance = (patternA, patternB) => {
  if (patternA.length !== patternB.length) return Infinity;
  let sum = 0;
  for (let i = 0; i < patternA.length; i++) {
    sum += Math.pow(patternA[i] - patternB[i], 2);
  }
  return Math.sqrt(sum);
};

// ==========================================
// 🚀 CONTROLLER: Contextual Pattern Search
// ==========================================
// @route   POST /api/analyze/pattern
const analyzePattern = async (req, res) => {
  try {
    const { currentPattern, timeframe } = req.body;

    // 1. Validation
    if (!currentPattern || currentPattern.length < 5) {
      return res.status(400).json({ message: 'Pattern array is too short.' });
    }

    // 2. Normalize Input
    const targetVector = normalizePattern(currentPattern);
    const patternLength = currentPattern.length;

    // 3. Fetch History
    const history = await Candle.find({ timeframe: timeframe || '1h' })
      .sort({ time: 1 })
      .select('close time -_id')
      .lean();

    if (history.length < 1000) {
      return res.status(400).json({ message: 'Not enough historical data.' });
    }

    let matches = [];
    const historyLimit = history.length - patternLength - FORECAST_HORIZON;

    // 4. Time Machine Scan
    for (let i = 0; i < historyLimit; i++) {
      const windowCandles = history.slice(i, i + patternLength);
      const windowPrices = windowCandles.map(c => c.close);
      const windowVector = normalizePattern(windowPrices); // Normalize history to match today
      const dist = calculateDistance(targetVector, windowVector);

      if (dist < SIMILARITY_THRESHOLD) {
        const entryPrice = history[i + patternLength - 1].close;
        const exitPrice = history[i + patternLength + FORECAST_HORIZON - 1].close;
        const percentChange = ((exitPrice - entryPrice) / entryPrice) * 100;

        matches.push({
          time: windowCandles[0].time, // Unix Timestamp
          similarity: dist,
          outcome: percentChange, 
        });
      }
    }

    // 5. Sort Matches
    matches.sort((a, b) => a.similarity - b.similarity);
    const topMatches = matches.slice(0, 20); 

    // =================================================================
    // 🧠 LOGIC UPDATE: PRECISE NEWS CONTEXT (+/- 2 Hours)
    // =================================================================
    const enrichedMatches = await Promise.all(topMatches.slice(0, 5).map(async (m) => {
      // Logic: Find news within 2 hours (7200s) of the match time
      // This confirms if the move was driven by fundamentals or purely technicals.
      const matchTime = m.time;
      
      const preciseNews = await NewsEvent.findOne({
        time: { 
          $gte: matchTime - 7200, // 2 hours before
          $lte: matchTime + 7200  // 2 hours after
        },
        currency: 'USD', // Focus on USD as requested
        impact: { $regex: /High/i } // Only High Impact
      }).select('event currency impact actual forecast');

      let contextLabel = "Technical Move"; // Default if no news found
      
      if (preciseNews) {
        contextLabel = `⚠️ ${preciseNews.event} (${preciseNews.currency})`;
        
        // Add Fundamental Bias Check
        if (preciseNews.actual && preciseNews.forecast) {
           const deviation = preciseNews.actual - preciseNews.forecast;
           contextLabel += ` [Dev: ${deviation.toFixed(1)}]`;
        }
      }

      return {
        ...m,
        newsContext: contextLabel,
        rawNews: preciseNews // Send full object in case frontend needs it
      };
    }));

    // 6. Calculate Probability
    let bullishCount = 0;
    let bearishCount = 0;

    topMatches.forEach(m => {
      if (m.outcome > 0) bullishCount++;
      else bearishCount++;
    });

    const total = topMatches.length;
    let probabilityUp = 0;
    let probabilityDown = 0;
    let sentiment = 'NEUTRAL';

    if (total > 0) {
      probabilityUp = ((bullishCount / total) * 100).toFixed(1);
      probabilityDown = ((bearishCount / total) * 100).toFixed(1);

      if (Number(probabilityUp) > 60) sentiment = 'BULLISH';
      else if (Number(probabilityDown) > 60) sentiment = 'BEARISH';
    }

    res.json({
      sentiment,
      probabilityUp: parseFloat(probabilityUp),
      probabilityDown: parseFloat(probabilityDown),
      matchesFound: matches.length,
      topMatches: enrichedMatches
    });

  } catch (error) {
    console.error("Fractal Analysis Error:", error);
    res.status(500).json({ message: 'Analysis Engine Failed' });
  }
};

module.exports = { analyzePattern };