const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');

// CONFIGURATION
const PATTERN_LENGTH = 8;        // The "DNA" length (Input size)
const FORECAST_HORIZON = 4;      // Look ahead 4 candles to determine win/loss
const SIMILARITY_THRESHOLD = 5.0; // Stricter = Lower number. 5.0 is a good balance for % based data.

// ==========================================
// 🧮 MATH ENGINE: Vector Normalization
// ==========================================
// Converts raw prices [2000, 2010, 2005] -> Percentage Moves [0, 0.5, 0.25]
// This allows matching 2004 patterns (low price) with 2026 patterns (high price).
const normalizePattern = (prices) => {
  if (!prices || prices.length === 0) return [];
  const basePrice = prices[0];
  // Multiply by 100 to get percentage points (e.g., 1.5%)
  return prices.map(p => ((p - basePrice) / basePrice) * 100);
};

// ==========================================
// 🧮 MATH ENGINE: Euclidean Distance
// ==========================================
// Calculates the "Shape Difference" between two patterns.
// 0 = Identical Shape.
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
    // Frontend sends: { currentPattern: [4950.5, 4951.2, ...], timeframe: '1h' }
    const { currentPattern, timeframe } = req.body;

    // 1. Validation
    if (!currentPattern || currentPattern.length < 5) {
      return res.status(400).json({ message: 'Pattern array is too short (min 5 candles).' });
    }

    // 2. Normalize the Input (The "Target" Vector)
    const targetVector = normalizePattern(currentPattern);
    const patternLength = currentPattern.length;

    // 3. Fetch History (Optimized Field Selection)
    // We only need 'close' and 'time' for the scan. 
    // .lean() makes it plain JS objects (faster).
    const history = await Candle.find({ timeframe: timeframe || '1h' })
      .sort({ time: 1 }) // Oldest -> Newest
      .select('close time -_id')
      .lean();

    if (history.length < 1000) {
      return res.status(400).json({ message: 'Not enough historical data to perform Fractal Analysis.' });
    }

    let matches = [];
    const historyLimit = history.length - patternLength - FORECAST_HORIZON;

    // 4. The Sliding Window Scan (The Time Machine)
    // Loop through 20 years of history...
    for (let i = 0; i < historyLimit; i++) {
      
      // Extract window
      const windowCandles = history.slice(i, i + patternLength);
      const windowPrices = windowCandles.map(c => c.close);
      
      // Normalize window to % change
      const windowVector = normalizePattern(windowPrices);

      // Compare Shapes (Math)
      const dist = calculateDistance(targetVector, windowVector);

      // 5. Filter for Matches
      if (dist < SIMILARITY_THRESHOLD) {
        
        // Determine Outcome (Next 4 Candles)
        const entryPrice = history[i + patternLength - 1].close; // End of pattern
        const exitPrice = history[i + patternLength + FORECAST_HORIZON - 1].close; // 4 periods later
        
        // Did price go UP or DOWN after this shape?
        const percentChange = ((exitPrice - entryPrice) / entryPrice) * 100;

        matches.push({
          time: windowCandles[0].time, // Unix Timestamp
          similarity: dist,
          outcome: percentChange, // +1.5% or -0.5%
        });
      }
    }

    // 6. Sort & Analyze Top Matches
    // Sort by Similarity (Lowest distance is best)
    matches.sort((a, b) => a.similarity - b.similarity);
    
    // Take Top 20 Twins
    const topMatches = matches.slice(0, 20); 

    let bullishCount = 0;
    let bearishCount = 0;

    // 7. Contextual Check (Did news cause this?)
    // We enrich the Top 5 matches with News Data for the frontend to display
    const enrichedMatches = await Promise.all(topMatches.slice(0, 5).map(async (m) => {
      // Check for High Impact news +/- 12 hours of the match
      const news = await NewsEvent.findOne({
        time: { $gte: m.time - 43200, $lte: m.time + 43200 },
        impact: { $regex: /High/i }
      }).select('event currency impact');

      return {
        ...m,
        newsContext: news ? `${news.event} (${news.currency})` : null
      };
    }));

    // Calculate Stats
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

    // 8. Return Analysis
    res.json({
      sentiment,
      probabilityUp: parseFloat(probabilityUp),
      probabilityDown: parseFloat(probabilityDown),
      matchesFound: matches.length, // "Found 142 similar patterns in history"
      topMatches: enrichedMatches   // "Here is what happened the last 5 times"
    });

  } catch (error) {
    console.error("Fractal Analysis Error:", error);
    res.status(500).json({ message: 'Analysis Engine Failed' });
  }
};

module.exports = { analyzePattern };