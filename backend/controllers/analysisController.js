const axios = require('axios');
const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');
const Trade = require('../models/Trade');
const { openPaperTradeIfEligible } = require('./tradeController');

// ⚙️ CONFIGURATION
const PYTHON_API_URL = process.env.PYTHON_API_URL || 'http://127.0.0.1:8000/api/analyze';

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

            // Purge any stale legacy OPEN trade with missing SL/TP, RR < 1.5, or opposite cycle direction
            const existingOpen = await Trade.findOne({ symbol: targetSymbol, status: 'OPEN' });
            if (existingOpen) {
                if (existingOpen.sl == null || existingOpen.tp == null) {
                    await Trade.deleteOne({ _id: existingOpen._id });
                } else {
                    const risk = Math.max(Math.abs(existingOpen.entry - existingOpen.sl), 1e-6);
                    const rr   = Math.abs(existingOpen.tp - existingOpen.entry) / risk;
                    const cycleIsBull = (aiResult.trend || '').toUpperCase().includes('BULLISH');
                    const tradeIsBuy  = existingOpen.action === 'Buy';

                    if (rr < 1.5 || cycleIsBull !== tradeIsBuy) {
                        await Trade.deleteOne({ _id: existingOpen._id });
                    }
                }
            }

            const activeTrade = await openPaperTradeIfEligible(targetSymbol, aiResult);

            // If an OPEN trade is active on this symbol, lock its exact Entry, SL, TP1, and TP2 into the response!
            if (activeTrade && activeTrade.status === 'OPEN') {
                const lockedEntry = Number(activeTrade.entry);
                const lockedSL    = Number(activeTrade.sl);
                const lockedTP1   = Number(activeTrade.tp);
                const rawTP2      = Number(aiResult.tradeSetup?.target_2 || aiResult.keyLevels?.target2 || lockedTP1);
                const isBuy       = activeTrade.action === 'Buy';
                const lockedTP2   = isBuy ? Math.max(lockedTP1, rawTP2) : Math.min(lockedTP1, rawTP2);

                const risk = Math.max(Math.abs(lockedEntry - lockedSL), 1e-6);
                const rr   = parseFloat((Math.max(Math.abs(lockedTP1 - lockedEntry), Math.abs(lockedTP2 - lockedEntry)) / risk).toFixed(2));
                const dir  = isBuy ? 'BUY' : 'SELL';

                aiResult.signal     = dir;
                aiResult.confidence = Math.max(aiResult.confidence || 0, activeTrade.confidence || 80);
                aiResult.tradeSetup = {
                    entry:       lockedEntry,
                    stop_loss:   lockedSL,
                    take_profit: lockedTP1,
                    target_1:    lockedTP1,
                    target_2:    lockedTP2,
                    risk_reward: rr
                };

                if (aiResult.keyLevels) {
                    aiResult.keyLevels.target1 = lockedTP1;
                    aiResult.keyLevels.target2 = lockedTP2;
                }

                aiResult.executionPlan = {
                    ...(aiResult.executionPlan || {}),
                    stage:             'TRADE_ACTIVE',
                    trigger_condition: `${isBuy ? '🟢 BUY' : '🔴 SELL'} EXECUTING: Locked Entry @ ${lockedEntry} | Target 1: ${lockedTP1} | Target 2: ${lockedTP2} | SL: ${lockedSL}`,
                    planned_entry:     lockedEntry,
                    planned_sl:        lockedSL,
                    planned_tp1:       lockedTP1,
                    planned_tp2:       lockedTP2,
                    risk_reward:       rr
                };

                if (Array.isArray(aiResult.checklist) && aiResult.checklist.length >= 4) {
                    aiResult.checklist[1].done   = true;
                    aiResult.checklist[1].detail = `Tapped @ ${lockedEntry} ✅`;
                    aiResult.checklist[2].done   = true;
                    aiResult.checklist[2].detail = 'Confirmed ✅';
                    aiResult.checklist[3].done   = true;
                    aiResult.checklist[3].detail = `1:${rr}`;
                }

                if (aiResult.visuals && Array.isArray(aiResult.visuals.projection_path) && aiResult.visuals.projection_path.length >= 3) {
                    aiResult.visuals.projection_path[0].price = lockedEntry;
                    aiResult.visuals.projection_path[1].price = lockedTP1;
                    aiResult.visuals.projection_path[2].price = lockedTP2;
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