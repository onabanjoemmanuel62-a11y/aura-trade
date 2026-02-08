const mongoose = require('mongoose');

const tradeSchema = new mongoose.Schema({
  pair: {
    type: String,
    required: true,
    default: 'XAUUSD'
  },
  action: {
    type: String,
    required: true,
    enum: ['Buy', 'Sell'] // Validation: It can only be Buy or Sell
  },
  entry: {
    type: Number,
    required: true
  },
  exit: {
    type: Number,
    required: true
  },
  result: {
    type: String,
    enum: ['WON', 'LOST'], // Validation: Standardizes status
    required: true
  },
  profit: {
    type: Number, // Can be negative for losses
    required: true
  },
  timestamp: {
    type: Date,
    default: Date.now
  }
});

module.exports = mongoose.model('Trade', tradeSchema);