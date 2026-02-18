const mongoose = require('mongoose');
const dotenv = require('dotenv');
const path = require('path');

// ✅ FIX: Import Class correctly
const YahooFinance = require('yahoo-finance2').default; 
const yahooFinance = new YahooFinance(); 

// 1. Setup Environment
if (require.main === module) {
    const envPath = path.join(__dirname, '../.env');
    console.log(`Loading .env from: ${envPath}`);
    dotenv.config({ path: envPath });

    if (!process.env.MONGO_URI) {
        console.error("❌ ERROR: .env loaded but MONGO_URI is missing!");
        process.exit(1);
    }
}

const Candle = require('../models/Candle');

// ⚙️ CONFIGURATION
const SYMBOL = 'GC=F'; // Gold Futures
const TIMEFRAMES = ['1h', '4h']; 

const backfillData = async () => {
    console.log(`🔌 Connecting to MongoDB Atlas...`);

    try {
        // ✅ FIX: Explicit connection options with longer timeouts
        await mongoose.connect(process.env.MONGO_URI, {
            serverSelectionTimeoutMS: 30000, // Wait 30s instead of 10s
            socketTimeoutMS: 45000,
        });
        console.log(`✅ DB Connected!`);
    } catch (err) {
        console.error("❌ DB Connection Failed. Check your IP Whitelist on Atlas!", err);
        process.exit(1);
    }

    console.log(`🚀 Starting GOLD FUTURES (GC=F) Backfill...`);

    // Calculate start date (60 days ago)
    const startDate = new Date();
    startDate.setDate(startDate.getDate() - 60); 
    const period1 = startDate.toISOString().split('T')[0];

    for (const interval of TIMEFRAMES) {
        console.log(`\n📡 Fetching ${interval} data from Yahoo Finance (Chart API)...`);

        try {
            const queryOptions = {
                period1: period1,
                interval: '1h' 
            };

            const result = await yahooFinance.chart(SYMBOL, queryOptions);
            const rawData = result.quotes; 

            if (!rawData || rawData.length === 0) {
                console.log(`⚠️ No data found for ${interval}`);
                continue;
            }

            console.log(`✅ Received ${rawData.length} candles. Saving to DB...`);

            const bulkOps = rawData.map((candle) => {
                if (!candle.date) return null;
                const timeInSeconds = Math.floor(new Date(candle.date).getTime() / 1000);

                return {
                    updateOne: {
                        filter: { time: timeInSeconds, timeframe: interval }, 
                        update: {
                            $set: {
                                time: timeInSeconds,
                                open: candle.open,
                                high: candle.high,
                                low: candle.low,
                                close: candle.close,
                                volume: candle.volume || 0,
                                timeframe: interval,
                                isWeekend: false
                            }
                        },
                        upsert: true
                    }
                };
            }).filter(op => op !== null);

            if (bulkOps.length > 0) {
                // ✅ FIX: Wait for the write to finish
                await Candle.bulkWrite(bulkOps);
                console.log(`💾 Saved ${bulkOps.length} candles (${interval}) to MongoDB.`);
            }

        } catch (error) {
            console.error(`❌ Failed to fetch ${interval}:`, error.message);
        }
    }

    console.log('\n🎉 SUCCESS: Gold History Loaded!');
    process.exit();
};

if (require.main === module) {
    backfillData();
}

module.exports = backfillData;