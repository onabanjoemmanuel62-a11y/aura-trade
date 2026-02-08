const mongoose = require('mongoose');

const candleSchema = new mongoose.Schema({
  time: { type: Number, required: true }, // Unix Timestamp (Seconds)
  open: { type: Number, required: true },
  high: { type: Number, required: true },
  low: { type: Number, required: true },
  close: { type: Number, required: true },
  timeframe: { type: String, required: true, index: true }, // '1m', '1h'
  
  // NEW FIELD: The Weekend Flag
  isWeekend: { type: Boolean, default: false } 
});

// Compound Index: Speeds up queries like "Give me 1h candles sorted by time"
candleSchema.index({ timeframe: 1, time: 1 });

module.exports = mongoose.model('Candle', candleSchema);