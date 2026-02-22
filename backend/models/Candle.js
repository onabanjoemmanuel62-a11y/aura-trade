const mongoose = require('mongoose');

const candleSchema = new mongoose.Schema({
  // 👈 NEW: Tracks which asset this candle belongs to (Defaults to Gold for legacy data compatibility)
  symbol: { type: String, required: true, index: true, default: 'GC=F' }, 
  
  time: { type: Number, required: true }, // Unix Timestamp (Seconds)
  open: { type: Number, required: true },
  high: { type: Number, required: true },
  low: { type: Number, required: true },
  close: { type: Number, required: true },
  timeframe: { type: String, required: true, index: true }, // '1m', '1h', '4h'
  isWeekend: { type: Boolean, default: false } 
});

// 👈 UPGRADED: Compound Index now includes the symbol. 
// Adding { unique: true } mathematically guarantees you will never have duplicate candles!
candleSchema.index({ symbol: 1, timeframe: 1, time: 1 }, { unique: true });

module.exports = mongoose.model('Candle', candleSchema);