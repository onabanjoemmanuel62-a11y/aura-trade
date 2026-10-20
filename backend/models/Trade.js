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
  sl: {
    type: Number,
    required: true
  },
  // Original stop-loss at entry so RR calculation never breaks after SL moves to Break-Even
  initialSl: {
    type: Number
  },
  // Flag indicating whether Stop Loss has been moved to Break-Even (+1 pip buffer)
  beTriggered: {
    type: Boolean,
    default: false
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
  exit: {
    type: Number
  },
  result: {
    type: String,
    enum: ['WON', 'LOST']
  },
  profit: {
    type: Number
  },
  timestamp: {
    type: Date,
    default: Date.now
  },
  closedAt: {
    type: Date
  }
});

tradeSchema.index({ symbol: 1, status: 1 });

module.exports = mongoose.model('Trade', tradeSchema);