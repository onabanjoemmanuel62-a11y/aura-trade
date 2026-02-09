const axios = require('axios');
const mongoose = require('mongoose');
const dotenv = require('dotenv');
const path = require('path');

// 1. Setup Environment
if (require.main === module) {
    // Point to backend/.env (Assuming running from backend/ folder)
    const envPath = path.join(__dirname, '../.env');
    console.log(`Loading .env from: ${envPath}`);
    dotenv.config({ path: envPath });

    if (!process.env.MONGO_URI) {
        console.error("❌ ERROR: .env loaded but MONGO_URI is missing!");
        process.exit(1);
    }

    const connectDB = require('../config/db');
    connectDB();
}

const Candle = require('../models/Candle');

// Configuration
const SYMBOL = 'PAXGUSDT'; // Gold-backed Crypto
const TIMEFRAMES = ['1h', '4h']; // <--- NOW SUPPORTS MULTIPLE
const LIMIT = 1000; // Get last 1000 candles

const backfillData = async () => {
    // Wait for DB connection
    if (mongoose.connection.readyState === 0) {
        await mongoose.connect(process.env.MONGO_URI);
    }

    console.log(`🚀 Starting Multi-Timeframe Backfill for ${SYMBOL}...`);

    for (const interval of TIMEFRAMES) {
        console.log(`\n📡 Fetching ${interval} data from Binance...`);

        try {
            // 2. Fetch Data from Binance Public API
            const url = `https://api.binance.com/api/v3/klines?symbol=${SYMBOL}&interval=${interval}&limit=${LIMIT}`;
            const response = await axios.get(url);
            const rawData = response.data;

            console.log(`✅ Received ${rawData.length} candles for ${interval}. Processing...`);

            // 3. Prepare Bulk Operations
            const bulkOps = rawData.map((candle) => {
                // Binance Format: [Timestamp(ms), Open, High, Low, Close, Volume, ...]
                const timeInSeconds = Math.floor(candle[0] / 1000); 

                return {
                    updateOne: {
                        filter: { time: timeInSeconds, timeframe: interval }, // <--- Specific to interval
                        update: {
                            $set: {
                                time: timeInSeconds,
                                open: parseFloat(candle[1]),
                                high: parseFloat(candle[2]),
                                low: parseFloat(candle[3]),
                                close: parseFloat(candle[4]),
                                timeframe: interval, // <--- Save as '1h' or '4h'
                                isWeekend: new Date(timeInSeconds * 1000).getDay() === 0 || new Date(timeInSeconds * 1000).getDay() === 6
                            }
                        },
                        upsert: true
                    }
                };
            });

            // 4. Execute Bulk Write
            if (bulkOps.length > 0) {
                await Candle.bulkWrite(bulkOps);
                console.log(`💾 Saved ${bulkOps.length} candles (${interval}) to MongoDB.`);
            } else {
                console.log(`⚠️ No data to save for ${interval}.`);
            }

        } catch (error) {
            console.error(`❌ Failed to fetch ${interval}:`, error.message);
        }
    }

    console.log('\n🎉 SUCCESS: All timeframes backfilled!');
    process.exit();
};

// Run immediately
if (require.main === module) {
    backfillData();
}

module.exports = backfillData;