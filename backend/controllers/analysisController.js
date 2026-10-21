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

            // Purge any stale legacy OPEN trade with missing SL/TP or opposite cycle direction
            // (Safe for Break-Even and TP1-trailed trades!)
            const existingOpen = await Trade.findOne({ symbol: targetSymbol, status: 'OPEN' });
            if (existingOpen) {
                if (existingOpen.sl == null || existingOpen.tp == null) {
                    await Trade.deleteOne({ _id: existingOpen._id });
                } else {
                    const pipBuf      = getPipBuffer(targetSymbol);
                    const baseSL      = Number(existingOpen.initialSl || existingOpen.sl);
                    const isAlreadyBE = Boolean(existingOpen.beTriggered || existingOpen.tp1Hit) || Math.abs(existingOpen.entry - existingOpen.sl) <= (pipBuf * 5);
                    const risk        = Math.max(Math.abs(existingOpen.entry - baseSL), 1e-6);
                    const maxTarget   = Number(existingOpen.tp2 || existingOpen.tp);
                    const rr          = Math.max(Math.abs(existingOpen.tp - existingOpen.entry), Math.abs(maxTarget - existingOpen.entry)) / risk;
                    const cycleIsBull = (aiResult.trend || '').toUpperCase().includes('BULLISH');
                    const tradeIsBuy  = (existingOpen.action || '').toUpperCase() === 'BUY';

                    if ((!isAlreadyBE && rr < 1.5) || cycleIsBull !== tradeIsBuy) {
                        await Trade.deleteOne({ _id: existingOpen._id });
                    }
                }
            }

            const activeTrade = await openPaperTradeIfEligible(targetSymbol, aiResult);

            // If an OPEN trade is active on this symbol, lock its exact Entry, SL (or BE/TP1-trailed SL), TP1, and TP2!
            if (activeTrade && activeTrade.status === 'OPEN') {
                const decimals    = getDecimals(targetSymbol);
                const pipBuf      = getPipBuffer(targetSymbol);
                const lockedEntry = Number(activeTrade.entry);
                let lockedSL      = Number(activeTrade.sl);
                const initSL      = Number(activeTrade.initialSl || aiResult.tradeSetup?.initial_sl || lockedSL);
                const lockedTP1   = Number(activeTrade.tp);
                const rawTP2      = Number(activeTrade.tp2 || aiResult.tradeSetup?.target_2 || aiResult.keyLevels?.target2 || lockedTP1);
                const isBuy       = (activeTrade.action || '').toUpperCase() === 'BUY';
                const lockedTP2   = isBuy ? Math.max(lockedTP1, rawTP2) : Math.min(lockedTP1, rawTP2);

                const pyBE        = Boolean(aiResult.tradeSetup?.breakeven_active);
                const pyTP1Hit    = Boolean(aiResult.tradeSetup?.tp1_hit);
                const initRisk    = Math.abs(lockedEntry - initSL);
                const tp1Dist     = Math.abs(lockedTP1 - lockedEntry);
                const beThreshold = Math.min(initRisk > pipBuf * 3 ? initRisk : tp1Dist * 0.35, tp1Dist * 0.50);

                let isTP1Hit   = Boolean(activeTrade.tp1Hit) || pyTP1Hit;
                let isBEActive = Boolean(activeTrade.beTriggered) || pyBE || isTP1Hit || (Math.abs(lockedEntry - lockedSL) <= pipBuf * 5);

                if (!isTP1Hit && ((isBuy && currentPrice >= lockedTP1) || (!isBuy && currentPrice <= lockedTP1))) {
                    isTP1Hit   = true;
                    isBEActive = true;
                } else if (!isBEActive && beThreshold > 0) {
                    if ((isBuy && (currentPrice - lockedEntry) >= beThreshold) || (!isBuy && (lockedEntry - currentPrice) >= beThreshold)) {
                        isBEActive = true;
                    }
                }

                let dbUpdated = false;
                if (isTP1Hit) {
                    const trailSL = Number((isBuy
                        ? lockedEntry + Math.max(pipBuf * 2, tp1Dist * 0.50)
                        : lockedEntry - Math.max(pipBuf * 2, tp1Dist * 0.50)
                    ).toFixed(decimals));
                    if ((isBuy && lockedSL < trailSL) || (!isBuy && lockedSL > trailSL)) {
                        lockedSL = trailSL;
                        activeTrade.sl = trailSL;
                        dbUpdated = true;
                    }
                    if (!activeTrade.tp1Hit) {
                        activeTrade.tp1Hit      = true;
                        activeTrade.beTriggered = true;
                        dbUpdated = true;
                    }
                } else if (isBEActive) {
                    const beSL = Number((isBuy ? lockedEntry + pipBuf : lockedEntry - pipBuf).toFixed(decimals));
                    if ((isBuy && lockedSL < beSL) || (!isBuy && lockedSL > beSL)) {
                        lockedSL = beSL;
                        activeTrade.sl = beSL;
                        dbUpdated = true;
                    }
                    if (!activeTrade.beTriggered) {
                        activeTrade.beTriggered = true;
                        dbUpdated = true;
                    }
                }

                if (dbUpdated && typeof activeTrade.save === 'function') {
                    await activeTrade.save();
                }

                const effRisk = initRisk > pipBuf * 3 ? initRisk : Math.max(tp1Dist / 2.0, 1e-6);
                const rr      = aiResult.tradeSetup?.risk_reward || parseFloat((Math.max(Math.abs(lockedTP1 - lockedEntry), Math.abs(lockedTP2 - lockedEntry)) / effRisk).toFixed(2));
                const dir     = isBuy ? 'BUY' : 'SELL';
                const slTag   = isTP1Hit
                    ? `SL (Trailed 🛡️): ${lockedSL}`
                    : isBEActive
                    ? `SL (BE 🛡️): ${lockedSL}`
                    : `SL: ${lockedSL}`;
                const tp1Tag  = isTP1Hit ? `TP1: ${lockedTP1} (HIT ✅)` : `Target 1: ${lockedTP1}`;

                aiResult.signal     = dir;
                aiResult.confidence = Math.max(aiResult.confidence || 0, activeTrade.confidence || 84);
                aiResult.tradeSetup = {
                    entry:            lockedEntry,
                    stop_loss:        lockedSL,
                    initial_sl:       initSL,
                    take_profit:      lockedTP1,
                    target_1:         lockedTP1,
                    target_2:         lockedTP2,
                    risk_reward:      rr,
                    breakeven_active: isBEActive,
                    tp1_hit:          isTP1Hit
                };

                if (aiResult.keyLevels) {
                    aiResult.keyLevels.target1 = lockedTP1;
                    aiResult.keyLevels.target2 = lockedTP2;
                }

                aiResult.executionPlan = {
                    ...(aiResult.executionPlan || {}),
                    stage:             'TRADE_ACTIVE',
                    trigger_condition: `${isBuy ? '🟢 BUY' : '🔴 SELL'} EXECUTING: Locked Entry @ ${lockedEntry} | ${tp1Tag} | Target 2: ${lockedTP2} | ${slTag}`,
                    planned_entry:     lockedEntry,
                    planned_sl:        lockedSL,
                    initial_sl:        initSL,
                    planned_tp1:       lockedTP1,
                    planned_tp2:       lockedTP2,
                    risk_reward:       rr,
                    breakeven_active:  isBEActive,
                    tp1_hit:           isTP1Hit
                };

                if (Array.isArray(aiResult.checklist) && aiResult.checklist.length >= 4) {
                    aiResult.checklist[1].done   = true;
                    aiResult.checklist[1].detail = `Tapped @ ${lockedEntry} ✅`;
                    aiResult.checklist[2].done   = true;
                    aiResult.checklist[2].detail = 'Confirmed ✅';
                    aiResult.checklist[3].done   = true;
                    aiResult.checklist[3].detail = isTP1Hit
                        ? `TP1 Hit ✅ -> Running to TP2`
                        : isBEActive
                        ? `1:${rr} (SL @ BE 🛡️)`
                        : `1:${rr}`;
                }

                if (aiResult.visuals && Array.isArray(aiResult.visuals.projection_path) && aiResult.visuals.projection_path.length >= 3) {
                    aiResult.visuals.projection_path[0].price = currentPrice;
                    aiResult.visuals.projection_path[1].price = isTP1Hit ? currentPrice : lockedTP1;
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