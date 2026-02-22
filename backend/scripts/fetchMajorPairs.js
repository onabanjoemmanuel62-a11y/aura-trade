const mongoose = require('mongoose');
const dotenv = require('dotenv');
const path = require('path');
const YahooFinance = require('yahoo-finance2').default;
const yahooFinance = new YahooFinance(); // 👈 THE FIX: Instantiate the v3 client
const Candle = require('../models/Candle');

dotenv.config({ path: path.join(__dirname, '../.env') }); 

// THE COMPLETE LIST: Gold + All 7 Forex Majors
const PAIRS = ['GC=F', 'EURUSD=X', 'GBPUSD=X', 'JPY=X', 'CHF=X', 'AUDUSD=X', 'CAD=X', 'NZDUSD=X'];
const TIMEFRAMES = { '1h': 3600, '4h': 14400 };

// Prevent saving weekend ghost candles
const isMarketClosed = (timestampInSeconds) => {
    const date = new Date(timestampInSeconds * 1000);
    const day = date.getUTCDay(); 
    const hour = date.getUTCHours(); 
    if (day === 6) return true; // Saturday
    if (day === 5 && hour >= 22) return true; // Friday post-close
    if (day === 0 && hour < 22) return true; // Sunday pre-open
    return false; 
};

const getBucketTime = (timestamp, timeframe) => {
    return timestamp - (timestamp % TIMEFRAMES[timeframe]);
};

const harvestData = async () => {
    try {
        console.log("🔌 Connecting to MongoDB...");
        await mongoose.connect(process.env.MONGO_URI);
        console.log("✅ Connected! Starting Complete Multi-Asset Harvest...");

        // Fetch the last 60 days (Yahoo's limit for 1h/4h data)
        const startDate = new Date();
        startDate.setDate(startDate.getDate() - 720);
        const period1 = startDate.toISOString().split('T')[0];

        for (const symbol of PAIRS) {
            console.log(`\n⏳ Fetching data for: ${symbol}...`);
            
            try {
                // 👇 FIX: Use the instantiated 'yahooFinance' client
                const result1h = await yahooFinance.chart(symbol, { period1, interval: '1h' });
                const quotes1h = result1h?.quotes || [];
                
                for (const tf of ['1h', '4h']) {
                    const bulkOps = quotes1h.map(candle => {
                        if (!candle.date || !candle.close) return null;
                        const timeInSeconds = Math.floor(new Date(candle.date).getTime() / 1000);
                        const bucketTime = getBucketTime(timeInSeconds, tf);

                        return {
                            updateOne: {
                                filter: { symbol: symbol, time: bucketTime, timeframe: tf },
                                update: {
                                    $max: { high: candle.high },
                                    $min: { low: candle.low },
                                    $set: { close: candle.close, isWeekend: isMarketClosed(timeInSeconds) },
                                    $setOnInsert: { open: candle.open, time: bucketTime, timeframe: tf, symbol: symbol }
                                },
                                upsert: true
                            }
                        };
                    }).filter(Boolean);

                    if (bulkOps.length > 0) {
                        await Candle.bulkWrite(bulkOps);
                        console.log(`✅ Saved ${bulkOps.length} [${tf}] candles for ${symbol}`);
                    }
                }
            } catch (fetchErr) {
                console.error(`⚠️ Could not fetch data for ${symbol}:`, fetchErr.message);
            }
        }

        console.log("\n🎉 MULTI-ASSET HARVEST COMPLETE!");
        process.exit(0);

    } catch (error) {
        console.error("❌ Harvest Failed:", error);
        process.exit(1);
    }
};

harvestData();