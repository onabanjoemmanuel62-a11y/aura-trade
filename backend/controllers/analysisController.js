const axios = require('axios');
const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');
const Trade = require('../models/Trade');
const { openPaperTradeIfEligible } = require('./tradeController');

// ⚙️ CONFIGURATION
const PYTHON_API_URL = process.env.PYTHON_API_URL || 'http://127.0.0.1:8000/api/analyze';

const getPipBuffer = (sym = '') => {
    const s = sym.toUpperCase();
    if (s.includes('GC=F') || s.includes('XAU') || s.includes('GOLD')) return 0.20;
    if (s.includes('JPY')) return 0.01;
    return 0.0001;
};

const getDecimals = (sym = '') => {
    const s = sym.toUpperCase();
    if (s.includes('GC=F') || s.includes('XAU') || s.includes('GOLD')) return 2;
    if (s.includes('JPY')) return 3;
    return 5;
};

// ==========================================
// 🕵️ NEWS ANALYZER (UNCHANGED LEGACY)
// ==========================================
const analyzeNewsImpact = async (targetEvent, targetCurrency) => {
    try {
        if (!targetEvent) return null;

        const pastEvents = await NewsEvent.find({
            event: targetEvent,
            currency: targetCurrency,
            actual: { $ne: null },
            forecast: { $ne: null }
        }).sort({ time: -1 }).limit(50);

        if (pastEvents.length < 3) return null;

        let logicalMoves = 0;
        let fakeouts = 0;
        let totalValid = 0;

        for (const news of pastEvents) {
            const deviation = news.actual - news.forecast;
            if (deviation === 0) continue;

            const candleTime = news.time - (news.time % 3600);
            const candle = await Candle.findOne({ time: candleTime, timeframe: '1h', symbol: 'GC=F' });
            if (!candle) continue;

            totalValid++;
            const marketMove = candle.close - candle.open;
            const isMarketGreen = marketMove > 0;
            const isNewsPositive = deviation > 0;

            if ((isNewsPositive && isMarketGreen) || (!isNewsPositive && !isMarketGreen)) {
                logicalMoves++;
            } else {
                fakeouts++;
            }
        }

        if (totalValid === 0) return null;

        const winRate = (logicalMoves / totalValid) * 100;
        let signal = 'NEUTRAL';
        if (winRate > 65) signal = 'FOLLOW_NEWS';
        if (winRate < 35) signal = 'INVERSE_NEWS';

        return {
            signal: signal === 'FOLLOW_NEWS' ? 'HIGH RELIABILITY' : 'CAUTION',
            probability: Math.round(winRate),
            reason: `Analyzed ${totalValid} past events.\nMarket followed news logic ${Math.round(winRate)}% of the time.`,
            eventName: targetEvent,
            stats: { wins: logicalMoves, losses: fakeouts }
        };

    } catch (err) {
        console.error("❌ News Analysis Error:", err);
        return null;
    }
};

// ==========================================
// 🚀 MAIN CONTROLLER (MULTI-TIMEFRAME MATRIX)
// ==========================================
exports.analyzePattern = async (req, res) => {
    try {
        const { timeframe, eventName, currency, symbol } = req.body;
        const targetSymbol = symbol || currency || 'GC=F';

        if (eventName) {
            const newsStrategy = await analyzeNewsImpact(eventName, currency || 'USD');
            if (newsStrategy && newsStrategy.probability > 60) {
                return res.json({
                    signal: newsStrategy.probability > 60 ? 'STRONG CORRELATION' : 'WEAK',
                    confidence: newsStrategy.probability,
                    pattern: 'Historical News Bias',
                    trend: 'News Driven',
                    reason: [newsStrategy.reason],
                    newsContext: `Stats: ${newsStrategy.stats.wins} Wins / ${newsStrategy.stats.losses} Losses`
                });
            }
        }

        const [raw1HCandles, raw4HCandles] = await Promise.all([
             Candle.find({ timeframe: '1h', symbol: targetSymbol }).sort({ time: -1 }).limit(3000).lean(),
             Candle.find({ timeframe: '4h', symbol: targetSymbol }).sort({ time: -1 }).limit(1000).lean()
        ]);

        if (raw1HCandles.length < 100 || raw4HCandles.length < 50) {
            return res.json({ signal: 'NEUTRAL', confidence: 0, reason: [`Not enough data for ${targetSymbol}.`] });
        }

        const formatCandles = (candles) => candles.reverse().map(c => ({
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
            time: c.time,
            volume: c.volume || 0
        }));

        const clean1HCandles = formatCandles(raw1HCandles);
        const clean4HCandles = formatCandles(raw4HCandles);
        const currentPrice = clean1HCandles[clean1HCandles.length - 1].close;

        const latestNews = await NewsEvent.findOne({
            impact: 'High',
            actual: { $ne: null },
            forecast: { $ne: null }
        }).sort({ time: -1 }).lean();

        try {
            const pythonResponse = await axios.post(PYTHON_API_URL, {
                timeframe: timeframe || '1h',
                currency: targetSymbol,
                current_price: currentPrice,
                candles: clean1HCandles,
                htf_candles: clean4HCandles,
                news_data: latestNews
            });

            const aiResult = pythonResponse.data;

            // 1. Purge any invalid legacy OPEN trade (missing SL/TP or opposite cycle direction)
            const existingOpen = await Trade.findOne({ symbol: targetSymbol, status: 'OPEN' });
            if (existingOpen) {
                if (existingOpen.sl == null || existingOpen.tp == null) {
                    await Trade.deleteOne({ _id: existingOpen._id });
                } else {
                    const baseSL      = Number(existingOpen.initialSl || existingOpen.sl);
                    const isAlreadyBE = Boolean(existingOpen.beTriggered);
                    const risk        = Math.max(Math.abs(existingOpen.entry - baseSL), 1e-6);
                    const rr          = Math.abs(existingOpen.tp - existingOpen.entry) / risk;
                    const cycleIsBull = (aiResult.trend || '').toUpperCase().includes('BULLISH');
                    const tradeIsBuy  = (existingOpen.action || '').toUpperCase() === 'BUY';

                    if ((!isAlreadyBE && rr < 1.5) || cycleIsBull !== tradeIsBuy) {
                        await Trade.deleteOne({ _id: existingOpen._id });
                    }
                }
            }

            // 2. Open paper trade (or retrieve existing OPEN trade; returns null if already taken & closed!)
            const activeTrade = await openPaperTradeIfEligible(targetSymbol, aiResult);

            // 3. If Python returned BUY/SELL on an older candle that was ALREADY closed in MongoDB,
            // downgrade response to WAIT so the chart never shows a ghost/duplicate trade!
            if (!activeTrade && (aiResult.signal === 'BUY' || aiResult.signal === 'SELL')) {
                aiResult.signal     = 'WAIT';
                aiResult.confidence = 58;
                aiResult.tradeSetup = null;
                if (aiResult.executionPlan) {
                    aiResult.executionPlan.stage = 'AWAITING_PULLBACK';
                    aiResult.executionPlan.trigger_condition = '⏳ Previous setup on this POI already completed. Waiting for next fresh structure break.';
                }
                if (aiResult.visuals) {
                    aiResult.visuals.projection_path = [];
                }
                return res.json(aiResult);
            }

            // 4. If a genuine OPEN trade is active, sync its locked MongoDB values directly into the response
            // (Break-Even is managed exclusively in tradeController.evaluateOpenTrades at 65% TP / +0.25R!)
            if (activeTrade && activeTrade.status === 'OPEN') {
                const decimals    = getDecimals(targetSymbol);
                const pipBuf      = getPipBuffer(targetSymbol);
                const lockedEntry = Number(activeTrade.entry);
                const lockedSL    = Number(activeTrade.sl);
                const initSL      = Number(activeTrade.initialSl || lockedSL);
                const lockedTP    = Number(activeTrade.tp);
                const isBuy       = (activeTrade.action || '').toUpperCase() === 'BUY';
                const isBEActive  = Boolean(activeTrade.beTriggered);

                const initRisk = Math.abs(lockedEntry - initSL);
                const tpDist   = Math.abs(lockedTP - lockedEntry);
                const effRisk  = initRisk > pipBuf * 3 ? initRisk : Math.max(tpDist / 2.0, 1e-6);
                const rr       = parseFloat((tpDist / effRisk).toFixed(2));
                const dir      = isBuy ? 'BUY' : 'SELL';
                const slTag    = isBEActive ? `SL (BE 🛡️): ${lockedSL}` : `SL: ${lockedSL}`;

                aiResult.signal     = dir;
                aiResult.confidence = Math.max(aiResult.confidence || 0, activeTrade.confidence || 84);
                aiResult.tradeSetup = {
                    entry:            lockedEntry,
                    stop_loss:        lockedSL,
                    initial_sl:       initSL,
                    take_profit:      lockedTP,
                    target_1:         lockedTP,
                    target_2:         null,
                    risk_reward:      rr,
                    breakeven_active: isBEActive
                };

                if (aiResult.keyLevels) {
                    aiResult.keyLevels.target1 = lockedTP;
                    aiResult.keyLevels.target2 = null;
                }

                aiResult.executionPlan = {
                    ...(aiResult.executionPlan || {}),
                    stage:             'TRADE_ACTIVE',
                    trigger_condition: `${isBuy ? '🟢 BUY' : '🔴 SELL'} EXECUTING: Locked Entry @ ${lockedEntry} | TP: ${lockedTP} | ${slTag}`,
                    planned_entry:     lockedEntry,
                    planned_sl:        lockedSL,
                    initial_sl:        initSL,
                    planned_tp1:       lockedTP,
                    planned_tp2:       null,
                    risk_reward:       rr,
                    breakeven_active:  isBEActive
                };

                if (Array.isArray(aiResult.checklist) && aiResult.checklist.length >= 4) {
                    aiResult.checklist[1].done   = true;
                    aiResult.checklist[1].detail = `Tapped @ ${lockedEntry} ✅`;
                    aiResult.checklist[2].done   = true;
                    aiResult.checklist[2].detail = 'Confirmed ✅';
                    aiResult.checklist[3].done   = true;
                    aiResult.checklist[3].detail = isBEActive ? `1:${rr} (SL @ BE 🛡️)` : `1:${rr}`;
                }

                if (aiResult.visuals && Array.isArray(aiResult.visuals.projection_path) && aiResult.visuals.projection_path.length >= 3) {
                    const midPrice = Number(((currentPrice + lockedTP) / 2.0).toFixed(decimals));
                    aiResult.visuals.projection_path[0].price = currentPrice;
                    aiResult.visuals.projection_path[1].price = midPrice;
                    aiResult.visuals.projection_path[2].price = lockedTP;
                }
            }

            return res.json(aiResult);

        } catch (pyError) {
            console.error("⚠️ Python Brain Unreachable:", pyError.message);
            return res.json({
                signal: 'NEUTRAL',
                confidence: 0,
                reason: ['SMC Engine is offline. Please check Python service.'],
                error: pyError.message
            });
        }

    } catch (error) {
        console.error('❌ Analysis Error:', error);
        res.status(500).json({ error: 'Analysis Failed' });
    }
};