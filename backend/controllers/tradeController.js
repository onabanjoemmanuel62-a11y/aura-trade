const Trade = require('../models/Trade');

// @desc    Get all trades
// @route   GET /api/trades
// @access  Public
const getTrades = async (req, res) => {
  try {
    // Find all trades and sort by timestamp (descending: newest first)
    const trades = await Trade.find().sort({ timestamp: -1 });
    
    // Send the data back as a JSON response
    res.status(200).json(trades);
  } catch (error) {
    res.status(500).json({ message: error.message });
  }
};

module.exports = { getTrades };