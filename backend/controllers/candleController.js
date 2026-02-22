const Candle = require('../models/Candle');

// @desc    Get candles with Cursor-Based Pagination
// @route   GET /api/candles/:timeframe?limit=100&before=1738000000&symbol=GC=F
const getCandles = async (req, res) => {
  try {
    const { timeframe } = req.params;
    
    // 1. CONFIGURATION (Safe Defaults)
    // We default to 100 for speed, but allow the frontend to ask for more.
    const limit = parseInt(req.query.limit) || 100; 
    
    // 2. CURSOR LOGIC & SYMBOL (The "Before" Filter)
    const before = req.query.before; 
    
    // 👈 NEW: Grab the symbol from the query, default to Gold so nothing breaks
    const symbol = req.query.symbol || 'GC=F'; 

    // Build the Query Object
    let query = { timeframe, symbol }; // 👈 We now strictly filter by symbol!

    if (before) {
      query.time = { $lt: parseInt(before) };
    }

    // 3. FETCH STRATEGY: "Newest First"
    // We KEEP your critical .sort({ time: -1 }) logic.
    // This ensures we always get the latest data relative to our cursor.
    const candles = await Candle.find(query)
      .sort({ time: -1 }) // CRITICAL: Always Descending (Newest -> Oldest)
      .limit(limit);      // Take the chunk size

    // 4. REORDER FOR CHART
    // The Chart expects Oldest -> Newest (Left to Right).
    // Since we fetched Newest -> Oldest, we flip the array.
    const chronologicalCandles = candles.reverse();

    res.json(chronologicalCandles);

  } catch (error) {
    console.error("Error fetching candles:", error);
    res.status(500).json({ message: error.message });
  }
};

module.exports = { getCandles };