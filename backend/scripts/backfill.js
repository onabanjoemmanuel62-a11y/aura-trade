const axios = require('axios');
const mongoose = require('mongoose');
const dotenv = require('dotenv');
const path = require('path');

// 1. Setup Environment
if (require.main === module) {
    // 🔍 FIX: Point correctly to backend/.env (One level up, not two)
    const envPath = path.join(__dirname, '../.env');
    console.log(`Loading .env from: ${envPath}`);
    dotenv.config({ path: envPath });

    // Verify it loaded
    if (!process.env.MONGO_URI) {
        console.error("❌ ERROR: .env loaded but MONGO_URI is missing!");
        process.exit(1);
    }

    const connectDB = require('../config/db');
    connectDB();
}

const Candle = require('../models/Candle');

// Configuration
const SYMBOL = 'PAXGUSDT'; // Gold-backed Crypto (proxy for XAUUSD)
const INTERVAL = '1h';
const LIMIT = 1000; // Get last ~41 days of data

const backfillData = async () => {
    // Wait for DB connection to be ready
    if (mongoose.connection.readyState === 0) {
        await mongoose.connect(process.env.MONGO_URI);
    }

    console.log(`📡 Connecting to Binance API for ${SYMBOL} (${INTERVAL})...`);

    try {
        // 2. Fetch Data from Binance Public API
        const url = `https://api.binance.com/api/v3/klines?symbol=${SYMBOL}&interval=${INTERVAL}&limit=${LIMIT}`;
        const response = await axios.get(url);
        const rawData = response.data;

        console.log(`✅ Received ${rawData.length} candles. Processing...`);

        // 3. Prepare Bulk Operations
        const bulkOps = rawData.map((candle) => {
            // Binance Format: [Timestamp(ms), Open, High, Low, Close, Volume, ...]
            const timeInSeconds = Math.floor(candle[0] / 1000); // Convert ms to seconds

            return {
                updateOne: {
                    filter: { time: timeInSeconds, timeframe: INTERVAL },
                    update: {
                        $set: {
                            time: timeInSeconds,
                            open: parseFloat(candle[1]),
                            high: parseFloat(candle[2]),
                            low: parseFloat(candle[3]),
                            close: parseFloat(candle[4]),
                            timeframe: INTERVAL,
                            // Helper to identify weekend candles (approximate)
                            isWeekend: new Date(timeInSeconds * 1000).getDay() === 0 || new Date(timeInSeconds * 1000).getDay() === 6
                        }
                    },
                    upsert: true
                }
            };
        });

        // 4. Execute Bulk Write (Fastest Method)
        if (bulkOps.length > 0) {
            await Candle.bulkWrite(bulkOps);
            console.log(`💾 Successfully backfilled ${bulkOps.length} candles to MongoDB.`);
        } else {
            console.log('⚠️ No data to save.');
        }

        console.log('🎉 SUCCESS: Database is now populated and ready for Analysis.');
        process.exit();

    } catch (error) {
        console.error('❌ Backfill Failed:', error.message);
        process.exit(1);
    }
};

// Run immediately
if (require.main === module) {
    backfillData();
}

module.exports = backfillData;