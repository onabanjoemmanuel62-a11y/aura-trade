const mongoose = require('mongoose');

const tradeSchema = new mongoose.Schema({
  // Raw symbol code matching Candle documents & server.js's ASSETS list (e.g. 'GC=F', 'EURUSD=X')
  symbol: {
    type: String,
    required: true,
    default: 'GC=F'
  },
  // Human-readable label for the dashboard (e.g. 'XAUUSD'), derived from `symbol`
  pair: {
    type: String,
    required: true,
    default: 'XAUUSD'
  },
  action: {
    type: String,
    required: true,
    enum: ['Buy', 'Sell']
  },
  entry: {
    type: Number,
    required: true
  },
  // Stop-loss / take-profit levels the AI set when the trade was opened.
  // Required so an OPEN trade can actually be monitored and closed later.
  sl: {
    type: Number,
    required: true
  },
  tp: {
    type: Number,
    required: true
  },
  // Confidence score the AI Brain gave this signal at entry (0-99), for reference/analysis.
  confidence: {
    type: Number
  },
  // OPEN = still live, being monitored against price. CLOSED = resolved (hit SL or TP).
  status: {
    type: String,
    enum: ['OPEN', 'CLOSED'],
    default: 'OPEN',
    required: true
  },
  // The following three are only set once the trade closes — no longer required
  // up front, since a freshly-opened paper trade doesn't have an exit yet.
  exit: {
    type: Number
  },
  result: {
    type: String,
    enum: ['WON', 'LOST']
  },
  profit: {
    type: Number // price-unit move (entry vs exit), negative for losses. Matches the dashboard's "pips" stat.
  },
  timestamp: {
    type: Date,
    default: Date.now
  },
  closedAt: {
    type: Date
  }
});

// Fast lookup for "is there already an open trade on this symbol?" (used to avoid
// opening duplicate paper trades while one is still active) and for the monitoring
// loop's per-tick query of open trades by symbol.
tradeSchema.index({ symbol: 1, status: 1 });

module.exports = mongoose.model('Trade', tradeSchema);