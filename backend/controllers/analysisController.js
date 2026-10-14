const axios = require('axios');
const Candle = require('../models/Candle');
const NewsEvent = require('../models/NewsEvent');
const Trade = require('../models/Trade');
const { openPaperTradeIfEligible } = require('./tradeController');

const PYTHON_API_URL = process.env.PYTHON_API_URL || 'http://127.0.0.1:8000/api/analyze';

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

            // Check if there is an existing OPEN trade with bad legacy RR (< 1.5) or opposite cycle direction
            const existingOpen = await Trade.findOne({ symbol: targetSymbol, status: 'OPEN' });
            if (existingOpen) {
                const risk = Math.max(Math.abs(existingOpen.entry - existingOpen.sl), 1e-6);
                const rr   = Math.abs(existingOpen.tp - existingOpen.entry) / risk;
                const cycleIsBull = (aiResult.trend || '').toUpperCase().includes('BULLISH');
                const tradeIsBuy  = existingOpen.action === 'Buy';

                if (rr < 1.5 || cycleIsBull !== tradeIsBuy) {
                    // Purge stale legacy mid-air trade (like USD/JPY RR 1.09)
                    await Trade.deleteOne({ _id: existingOpen._id });
                }
            }

            const activeTrade = await openPaperTradeIfEligible(targetSymbol, aiResult);

            if (activeTrade && activeTrade.status === 'OPEN') {
                const risk = Math.max(Math.abs(activeTrade.entry - activeTrade.sl), 1e-6);
                const t1   = activeTrade.tp;
                const t2   = aiResult.keyLevels?.target2 || activeTrade.tp;
                const rr   = parseFloat((Math.max(Math.abs(t1 - activeTrade.entry), Math.abs(t2 - activeTrade.entry)) / risk).toFixed(2));

                aiResult.signal     = activeTrade.action.toUpperCase();
                aiResult.confidence = Math.max(aiResult.confidence || 0, activeTrade.confidence || 75);
                aiResult.tradeSetup = {
                    entry:       activeTrade.entry,
                    stop_loss:   activeTrade.sl,
                    take_profit: t1,
                    target_1:    t1,
                    target_2:    t2,
                    risk_reward: rr
                };

                // Align the projected arrow directly from current price -> Target 1 -> Target 2 while trade is active!
                if (aiResult.visuals && Array.isArray(aiResult.visuals.projection_path) && aiResult.visuals.projection_path.length >= 3) {
                    aiResult.visuals.projection_path[1].price = t1;
                    aiResult.visuals.projection_path[2].price = t2;
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