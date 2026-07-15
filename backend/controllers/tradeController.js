const Trade = require('../models/Trade');

// Maps the raw asset codes used internally (Candle.symbol, server.js's ASSETS list)
// to a human-readable pair label for the dashboard, matching the convention already
// used elsewhere in the app (e.g. brain.py's get_instrument_profile).
const SYMBOL_LABELS = {
  'GC=F': 'XAUUSD',
  'EURUSD=X': 'EURUSD',
  'GBPUSD=X': 'GBPUSD',
  'JPY=X': 'USDJPY',
  'AUDUSD=X': 'AUDUSD',
  'CAD=X': 'USDCAD',
  'CHF=X': 'USDCHF',
  'NZDUSD=X': 'NZDUSD',
};

const symbolToPairLabel = (symbol) => SYMBOL_LABELS[symbol] || symbol;

// @desc    Get trades (optionally filtered by status)
// @route   GET /api/trades?status=OPEN|CLOSED
// @access  Public
const getTrades = async (req, res) => {
  try {
    const filter = {};
    if (req.query.status) {
      filter.status = req.query.status.toUpperCase();
    }
    const trades = await Trade.find(filter).sort({ timestamp: -1 });
    res.status(200).json(trades);
  } catch (error) {
    res.status(500).json({ message: error.message });
  }
};

// @desc    Get only currently-open paper trades
// @route   GET /api/trades/open
// @access  Public
const getOpenTrades = async (req, res) => {
  try {
    const trades = await Trade.find({ status: 'OPEN' }).sort({ timestamp: -1 });
    res.status(200).json(trades);
  } catch (error) {
    res.status(500).json({ message: error.message });
  }
};

/**
 * Opens a new automatic paper trade from a qualifying AI signal.
 * Called internally by analysisController — NOT an HTTP route.
 *
 * Guards against duplicates: if a symbol already has an OPEN trade, this does
 * nothing (mirrors brain.py's own SIGNAL_LOCK concept, but persisted in Mongo
 * so it survives restarts — one open paper position per symbol at a time).
 *
 * @param {string} symbol - raw asset code, e.g. 'GC=F'
 * @param {object} analysisResult - the object returned by the Python brain's
 *   /api/analyze response (must have .signal, .tradeSetup, .confidence)
 */
const openPaperTradeIfEligible = async (symbol, analysisResult) => {
  try {
    if (!analysisResult) return null;
    const { signal, tradeSetup, confidence } = analysisResult;

    if (signal !== 'BUY' && signal !== 'SELL') return null;
    if (!tradeSetup || tradeSetup.entry == null || tradeSetup.stop_loss == null || tradeSetup.take_profit == null) return null;

    const existingOpen = await Trade.findOne({ symbol, status: 'OPEN' });
    if (existingOpen) {
      return null; // already have an open paper position on this symbol — don't stack another
    }

    const trade = await Trade.create({
      symbol,
      pair: symbolToPairLabel(symbol),
      action: signal === 'BUY' ? 'Buy' : 'Sell',
      entry: tradeSetup.entry,
      sl: tradeSetup.stop_loss,
      tp: tradeSetup.take_profit,
      confidence: confidence,
      status: 'OPEN',
    });

    console.log(`📝 PAPER TRADE OPENED: ${trade.action} ${trade.pair} @ ${trade.entry} | SL ${trade.sl} | TP ${trade.tp} | Confidence ${confidence}%`);
    return trade;
  } catch (error) {
    // Never let paper-trade bookkeeping break the actual analysis response.
    console.error(`⚠️ Failed to open paper trade for ${symbol}:`, error.message);
    return null;
  }
};

/**
 * Checks all OPEN paper trades on a given symbol against a fresh price candle,
 * closing any whose stop-loss or take-profit was hit. Called internally by
 * server.js's live price tick handler — NOT an HTTP route.
 *
 * Uses high/low (not just close) to catch intra-candle wicks touching SL/TP,
 * matching the same logic backtest.py uses to simulate trade outcomes —
 * keeps live paper trading consistent with how the strategy was validated.
 *
 * @param {string} symbol - raw asset code, e.g. 'GC=F'
 * @param {object} candle - { high, low, close, time } from the live price feed
 */
const evaluateOpenTrades = async (symbol, candle) => {
  try {
    if (!candle || candle.high == null || candle.low == null) return;

    const openTrades = await Trade.find({ symbol, status: 'OPEN' });
    if (openTrades.length === 0) return;

    for (const trade of openTrades) {
      let hit = null; // 'TP' | 'SL' | null
      let exitPrice = null;

      if (trade.action === 'Buy') {
        if (candle.low <= trade.sl) {
          hit = 'SL';
          exitPrice = trade.sl;
        } else if (candle.high >= trade.tp) {
          hit = 'TP';
          exitPrice = trade.tp;
        }
      } else { // 'Sell'
        if (candle.high >= trade.sl) {
          hit = 'SL';
          exitPrice = trade.sl;
        } else if (candle.low <= trade.tp) {
          hit = 'TP';
          exitPrice = trade.tp;
        }
      }

      if (!hit) continue;

      const profit = trade.action === 'Buy'
        ? exitPrice - trade.entry
        : trade.entry - exitPrice;

      trade.status = 'CLOSED';
      trade.exit = exitPrice;
      trade.result = hit === 'TP' ? 'WON' : 'LOST';
      trade.profit = profit;
      trade.closedAt = new Date();
      await trade.save();

      console.log(`✅ PAPER TRADE CLOSED: ${trade.action} ${trade.pair} | ${trade.result} | Exit ${exitPrice} | Profit ${profit.toFixed(2)}`);
    }
  } catch (error) {
    console.error(`⚠️ Failed to evaluate open trades for ${symbol}:`, error.message);
  }
};

module.exports = {
  getTrades,
  getOpenTrades,
  openPaperTradeIfEligible,
  evaluateOpenTrades,
  symbolToPairLabel,
};