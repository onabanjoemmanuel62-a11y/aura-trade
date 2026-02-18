const mongoose = require('mongoose');
const dotenv = require('dotenv');
const path = require('path');
const YahooFinance = require('yahoo-finance2').default; 
const yahooFinance = new YahooFinance(); 

// 1. Setup Environment
const envPath = path.join(__dirname, '../.env');
dotenv.config({ path: envPath });

const Candle = require('../models/Candle');

// ⚙️ CONFIGURATION
const SYMBOL = 'GC=F'; // Gold Futures (Clean Institutional Data)
const TIMEFRAMES = ['1h', '4h']; 

const nukeAndSync = async () => {
    console.log(`🔌 Connecting to MongoDB Atlas...`);
    try {
        await mongoose.connect(process.env.MONGO_URI, {
            serverSelectionTimeoutMS: 30000,
            socketTimeoutMS: 45000,
        });
        console.log(`✅ DB Connected!`);
    } catch (err) {
        console.error("❌ DB Connection Failed.", err);
        process.exit(1);
    }

    // 🔥 THE NUKE: Wipe all polluted/mixed data
    console.log(`\n☢️ WARNING: Wiping old polluted database history...`);
    await Candle.deleteMany({});
    console.log(`✅ Database is completely clean.`);

    console.log(`\n🚀 Starting Fresh GOLD (GC=F) Backfill...`);

    // Get 60 days of continuous data
    const startDate = new Date();
    startDate.setDate(startDate.getDate() - 60); 
    const period1 = startDate.toISOString().split('T')[0];

    for (const interval of TIMEFRAMES) {
        console.log(`📡 Fetching ${interval} data...`);

        try {
            const result = await yahooFinance.chart(SYMBOL, { period1, interval: '1h' });
            const rawData = result.quotes; 

            if (!rawData || rawData.length === 0) continue;

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
                await Candle.bulkWrite(bulkOps);
                console.log(`💾 Saved ${bulkOps.length} clean candles (${interval}).`);
            }

        } catch (error) {
            console.error(`❌ Failed to fetch ${interval}:`, error.message);
        }
    }

    console.log('\n🎉 SUCCESS: Clean Gold History Loaded!');
    process.exit();
};

nukeAndSync();