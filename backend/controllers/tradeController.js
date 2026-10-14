const Trade = require('../models/Trade');

const SYMBOL_LABELS = {
  'GC=F':     'XAUUSD',
  'EURUSD=X': 'EURUSD',
  'GBPUSD=X': 'GBPUSD',
  'JPY=X':    'USDJPY',
  'AUDUSD=X': 'AUDUSD',
  'CAD=X':    'USDCAD',
  'CHF=X':    'USDCHF',
  'NZDUSD=X': 'NZDUSD',
};

const symbolToPairLabel = (symbol) => SYMBOL_LABELS[symbol] || symbol;

const calculatePips = (symbol = '', rawPriceDiff = 0) => {
  const s = symbol.toUpperCase();
  let pips;
  if (s.includes('GC=F') || s.includes('XAU') || s.includes('GOLD')) {
    pips = rawPriceDiff * 10;
  } else if (s.includes('JPY')) {
    pips = rawPriceDiff * 100;
  } else {
    pips = rawPriceDiff * 10000;
  }
  return parseFloat(pips.toFixed(1));
};

// @desc    Get trades (auto-cleans legacy seed trades that have no SL/TP)
// @route   GET /api/trades?status=OPEN|CLOSED
// @access  Public
const getTrades = async (req, res) => {
  try {
    // 1. Automatically remove old dummy/seed trades that have no Stop Loss or Take Profit (e.g. XAUUSD @ 2030 with — / —)
    await Trade.deleteMany({
      $or: [
        { sl: { $exists: false } },
        { sl: null },
        { tp: { $exists: false } },
        { tp: null },
      ]
    });

    // 2. Fix any legacy trade where result is WON/LOST but status was left as OPEN
    await Trade.updateMany(
      { result: { $in: ['WON', 'LOST'] }, status: 'OPEN' },
      { $set: { status: 'CLOSED' } }
    );

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
    const trades = await Trade.find({
      status: 'OPEN',
      sl: { $ne: null },
      tp: { $ne: null }
    }).sort({ timestamp: -1 });
    res.status(200).json(trades);
  } catch (error) {
    res.status(500).json({ message: error.message });
  }
};

const openPaperTradeIfEligible = async (symbol, analysisResult) => {
  try {
    if (!analysisResult) return null;

    const existingOpen = await Trade.findOne({
      symbol,
      status: 'OPEN',
      sl: { $ne: null },
      tp: { $ne: null }
    });
    if (existingOpen) {
      return existingOpen;
    }

    const { signal, tradeSetup, confidence } = analysisResult;
    if (signal !== 'BUY' && signal !== 'SELL') return null;
    if ((confidence || 0) < 65) return null;
    if (!tradeSetup || tradeSetup.entry == null || tradeSetup.stop_loss == null || tradeSetup.take_profit == null) {
      return null;
    }

    const trade = await Trade.create({
      symbol,
      pair:       symbolToPairLabel(symbol),
      action:     signal === 'BUY' ? 'Buy' : 'Sell',
      entry:      tradeSetup.entry,
      sl:         tradeSetup.stop_loss,
      tp:         tradeSetup.take_profit,
      confidence: confidence,
      status:     'OPEN',
      result:     'PENDING',
    });

    console.log(`📝 PAPER TRADE OPENED: ${trade.action} ${trade.pair} @ ${trade.entry} | SL ${trade.sl} | TP ${trade.tp} | Confidence ${confidence}%`);
    return trade;
  } catch (error) {
    console.error(`⚠️ Failed to open paper trade for ${symbol}:`, error.message);
    return null;
  }
};

const evaluateOpenTrades = async (symbol, candle) => {
  try {
    if (!candle || candle.high == null || candle.low == null) return;

    const openTrades = await Trade.find({
      symbol,
      status: 'OPEN',
      sl: { $ne: null },
      tp: { $ne: null }
    });
    if (openTrades.length === 0) return;

    for (const trade of openTrades) {
      let hit = null;
      let exitPrice = null;

      if (trade.action === 'Buy') {
        if (candle.low <= trade.sl) {
          hit = 'SL';
          exitPrice = trade.sl;
        } else if (candle.high >= trade.tp) {
          hit = 'TP';
          exitPrice = trade.tp;
        }
      } else {
        if (candle.high >= trade.sl) {
          hit = 'SL';
          exitPrice = trade.sl;
        } else if (candle.low <= trade.tp) {
          hit = 'TP';
          exitPrice = trade.tp;
        }
      }

      if (!hit) continue;

      const rawDiff = trade.action === 'Buy'
        ? exitPrice - trade.entry
        : trade.entry - exitPrice;

      const profitPips = calculatePips(symbol, rawDiff);

      trade.status   = 'CLOSED';
      trade.exit     = exitPrice;
      trade.result   = hit === 'TP' ? 'WON' : 'LOST';
      trade.profit   = profitPips;
      trade.closedAt = new Date();
      await trade.save();

      console.log(`✅ PAPER TRADE CLOSED: ${trade.action} ${trade.pair} | ${trade.result} | Exit ${exitPrice} | Profit ${profitPips > 0 ? '+' : ''}${profitPips} pips`);
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
  calculatePips,
};