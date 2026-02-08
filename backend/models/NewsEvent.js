const mongoose = require('mongoose');

const newsEventSchema = new mongoose.Schema({
  originalId: { type: Number, unique: true }, // Prevents duplicates
  time: { type: Number, required: true, index: true }, // Unix Timestamp (Seconds)
  currency: { type: String, required: true, index: true }, // 'USD', 'EUR'
  event: { type: String, required: true }, // 'Non-Farm Employment Change'
  impact: { type: String, required: true }, // 'High Impact Expected'
  actual: { type: Number, default: null },
  forecast: { type: Number, default: null },
  previous: { type: Number, default: null }
});

// Compound Index: "Find all USD news that happened at 8:30 AM"
newsEventSchema.index({ time: 1, currency: 1 });

module.exports = mongoose.model('NewsEvent', newsEventSchema);