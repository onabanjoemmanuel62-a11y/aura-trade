const Trade = require('../models/Trade');

// Maps raw Yahoo Finance symbols to clean dashboard pair labels
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

// In-memory backup vault so an active trade's Entry/SL/TP NEVER shifts even if MongoDB hiccups
const MEMORY_OPEN_TRADES = {};

const getPipBuffer = (symbol = '') => {
  const s = symbol.toUpperCase();
  if (s.includes('GC=F') || s.includes('XAU') || s.includes('GOLD')) return 0.20;
  if (s.includes('JPY')) return 0.01;
  return 0.0001;
};

const getDecimals = (symbol = '') => {
  const s = symbol.toUpperCase();
  if (s.includes('GC=F') || s.includes('XAU') || s.includes('GOLD')) return 2;
  if (s.includes('JPY')) return 3;
  return 5;
};

/**
 * Converts a raw price difference into standard Forex / Gold Pips:
 * - Gold (GC=F / XAUUSD): $0.10 move = 1 pip (so $10 move = 100 pips)
 * - JPY pairs (JPY=X / USDJPY): 0.01 move = 1 pip
 * - Forex Majors (EURUSD, GBPUSD, etc.): 0.0001 move = 1 pip
 */
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
    await Trade.deleteMany({
      $or: [
        { sl: { $exists: false } },
        { sl: null },
        { tp: { $exists: false } },
        { tp: null },
      ]
    });

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

/**
 * Opens a new automatic paper trade from a qualifying AI signal, OR returns
 * the existing OPEN trade for this symbol so the chart keeps showing the
 * exact locked Entry / SL / TP1 / TP2 lines without shifting as price moves.
 */
const openPaperTradeIfEligible = async (symbol, analysisResult) => {
  try {
    if (!analysisResult) return null;

    // 1. Check MongoDB for an existing OPEN trade on this symbol
    const existingOpen = await Trade.findOne({
      symbol,
      status: 'OPEN',
      sl: { $ne: null },
      tp: { $ne: null }
    });
    if (existingOpen) {
      // Backfill tp2 if an older open trade did not have tp2 saved yet
      const incomingTp2 = Number(
        analysisResult.tradeSetup?.target_2 ||
        analysisResult.keyLevels?.target2 ||
        existingOpen.tp2 ||
        existingOpen.tp
      );
      if (!existingOpen.tp2 && incomingTp2) {
        existingOpen.tp2 = incomingTp2;
        await existingOpen.save();
      }
      if (!existingOpen.initialSl) {
        existingOpen.initialSl = Number(
          analysisResult.tradeSetup?.initial_sl || existingOpen.sl
        );
        await existingOpen.save();
      }
      MEMORY_OPEN_TRADES[symbol] = existingOpen;
      return existingOpen;
    }

    // 2. Check In-Memory Vault fallback
    if (MEMORY_OPEN_TRADES[symbol] && MEMORY_OPEN_TRADES[symbol].status === 'OPEN') {
      return MEMORY_OPEN_TRADES[symbol];
    }

    // 3. Otherwise, only open a new trade if Python fired a live BUY or SELL with >= 65% confidence
    const { signal, tradeSetup, confidence } = analysisResult;
    if (signal !== 'BUY' && signal !== 'SELL') return null;
    if ((confidence || 0) < 65) return null;
    if (!tradeSetup || tradeSetup.entry == null || tradeSetup.stop_loss == null || tradeSetup.take_profit == null) {
      return null;
    }

    const entryNum = Number(tradeSetup.entry);
    const slNum    = Number(tradeSetup.stop_loss);
    const initSl   = Number(tradeSetup.initial_sl || tradeSetup.stop_loss);
    const tp1Num   = Number(tradeSetup.target_1 || tradeSetup.take_profit);
    const tp2Num   = Number(tradeSetup.target_2 || tp1Num);

    const tradePayload = {
      symbol,
      pair:        symbolToPairLabel(symbol),
      action:      signal === 'BUY' ? 'Buy' : 'Sell',
      entry:       entryNum,
      sl:          slNum,
      initialSl:   initSl,
      beTriggered: Boolean(tradeSetup.breakeven_active),
      tp:          tp1Num,
      tp2:         tp2Num,
      tp1Hit:      Boolean(tradeSetup.tp1_hit),
      confidence:  Number(confidence),
      status:      'OPEN',
    };

    MEMORY_OPEN_TRADES[symbol] = tradePayload;

    const trade = await Trade.create(tradePayload);
    MEMORY_OPEN_TRADES[symbol] = trade;

    console.log(`📝 PAPER TRADE OPENED & LOCKED: ${trade.action} ${trade.pair} @ ${trade.entry} | SL ${trade.sl} | TP1 ${trade.tp} | TP2 ${trade.tp2} | Confidence ${confidence}%`);
    return trade;
  } catch (error) {
    console.error(`⚠️ DB save warning for ${symbol} (using in-memory lock):`, error.message);
    return MEMORY_OPEN_TRADES[symbol] || null;
  }
};

/**
 * 3-STAGE PAPER TRADE LIFECYCLE EVALUATOR (Every 30s):
 * - Stage 1 (1R / 50% TP1): Moves SL to Break-Even (Entry ± 1 pip buffer).
 * - Stage 2 (TP1 Reached): Does NOT close the trade! Marks `tp1Hit = true`,
 *   trails SL into profit (50% of TP1 distance), and lets the trade run toward TP2.
 * - Stage 3 (TP2 Reached or Trailed SL Hit):
 *   - If TP2 is hit -> closes at TP2 with full TP2 profit (WON).
 *   - If Trailed SL is hit after TP1 was already banked -> closes with TP1 profit (WON).
 *   - If SL is hit before TP1 -> closes at currentSL (WON if BE buffer, LOST if initial SL).
 */
const evaluateOpenTrades = async (symbol, candle) => {
  try {
    if (!candle || candle.high == null || candle.low == null) return;

    const pipBuf   = getPipBuffer(symbol);
    const decimals = getDecimals(symbol);

    const openTrades = await Trade.find({
      symbol,
      status: 'OPEN',
      sl: { $ne: null },
      tp: { $ne: null }
    });
    if (openTrades.length === 0) return;

    for (const trade of openTrades) {
      const isBuy     = (trade.action || '').toUpperCase() === 'BUY';
      const entry     = Number(trade.entry);
      let currentSL   = Number(trade.sl);
      const initSL    = Number(trade.initialSl || currentSL);
      const tp1       = Number(trade.tp);
      const tp2       = Number(trade.tp2 || tp1);
      const hasDistinctTP2 = isBuy ? (tp2 > tp1 + pipBuf * 2) : (tp2 < tp1 - pipBuf * 2);

      let stateChanged = false;

      // Stage 1: Adaptive Break-Even Check (1R or 50% of TP1 distance)
      const initRisk = Math.abs(entry - initSL);
      const tp1Dist  = Math.abs(tp1 - entry);

      if (!trade.beTriggered && tp1Dist > 0) {
        const beTriggerDist = Math.min(initRisk > pipBuf * 3 ? initRisk : tp1Dist * 0.35, tp1Dist * 0.50);
        if (isBuy && candle.high >= entry + beTriggerDist) {
          const beSL = Number((entry + pipBuf).toFixed(decimals));
          if (currentSL < beSL) {
            currentSL = beSL;
            trade.sl  = beSL;
          }
          trade.beTriggered = true;
          stateChanged = true;
          console.log(`🛡️ BREAK-EVEN LOCKED: BUY ${trade.pair} SL moved to ${currentSL}`);
        } else if (!isBuy && candle.low <= entry - beTriggerDist) {
          const beSL = Number((entry - pipBuf).toFixed(decimals));
          if (currentSL > beSL) {
            currentSL = beSL;
            trade.sl  = beSL;
          }
          trade.beTriggered = true;
          stateChanged = true;
          console.log(`🛡️ BREAK-EVEN LOCKED: SELL ${trade.pair} SL moved to ${currentSL}`);
        }
      }

      // Stage 2: Check if TP1 was reached (Bank TP1 & Trail SL for TP2 Runner!)
      if (!trade.tp1Hit && hasDistinctTP2) {
        if (isBuy && candle.high >= tp1) {
          trade.tp1Hit      = true;
          trade.beTriggered = true;
          const trailSL     = Number((entry + Math.max(pipBuf * 2, tp1Dist * 0.50)).toFixed(decimals));
          if (currentSL < trailSL) {
            currentSL = trailSL;
            trade.sl  = trailSL;
          }
          stateChanged = true;
          console.log(`🎯 TP1 BANKED (${tp1}) on BUY ${trade.pair}! Running to TP2 (${tp2}) with trailed SL @ ${currentSL}`);
        } else if (!isBuy && candle.low <= tp1) {
          trade.tp1Hit      = true;
          trade.beTriggered = true;
          const trailSL     = Number((entry - Math.max(pipBuf * 2, tp1Dist * 0.50)).toFixed(decimals));
          if (currentSL > trailSL) {
            currentSL = trailSL;
            trade.sl  = trailSL;
          }
          stateChanged = true;
          console.log(`🎯 TP1 BANKED (${tp1}) on SELL ${trade.pair}! Running to TP2 (${tp2}) with trailed SL @ ${currentSL}`);
        }
      }

      // Stage 3: Check Final Exit (TP2 Hit, or SL Hit)
      let hit = null;
      let exitPrice = null;
      const finalTarget = hasDistinctTP2 ? tp2 : tp1;

      if (isBuy) {
        if (candle.high >= finalTarget) {
          hit = 'TP2';
          exitPrice = finalTarget;
        } else if (candle.low <= currentSL) {
          hit = 'SL';
          // If TP1 was already banked earlier, credit TP1 profit; otherwise exit at currentSL
          exitPrice = trade.tp1Hit ? tp1 : currentSL;
        }
      } else {
        if (candle.low <= finalTarget) {
          hit = 'TP2';
          exitPrice = finalTarget;
        } else if (candle.high >= currentSL) {
          hit = 'SL';
          exitPrice = trade.tp1Hit ? tp1 : currentSL;
        }
      }

      if (!hit) {
        if (stateChanged) {
          await trade.save();
          MEMORY_OPEN_TRADES[symbol] = trade;
        }
        continue;
      }

      const rawDiff = isBuy
        ? exitPrice - entry
        : entry - exitPrice;

      const profitPips = calculatePips(symbol, rawDiff);

      trade.status   = 'CLOSED';
      trade.exit     = exitPrice;
      trade.result   = (profitPips >= 0 || trade.tp1Hit) ? 'WON' : 'LOST';
      trade.profit   = profitPips;
      trade.closedAt = new Date();
      await trade.save();

      delete MEMORY_OPEN_TRADES[symbol];

      console.log(`✅ PAPER TRADE CLOSED (${hit}): ${trade.action} ${trade.pair} | ${trade.result} | Exit ${exitPrice} | Profit ${profitPips > 0 ? '+' : ''}${profitPips} pips`);
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