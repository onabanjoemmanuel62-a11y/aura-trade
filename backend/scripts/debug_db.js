const mongoose = require('mongoose');
const dotenv = require('dotenv');
const path = require('path');
const Candle = require('../models/Candle');

// Load Env
const envPath = path.join(__dirname, '../.env');
dotenv.config({ path: envPath });

const runDebug = async () => {
    try {
        console.log("🔌 Connecting to DB...");
        await mongoose.connect(process.env.MONGO_URI);
        console.log("✅ Connected.");

        // 1. Check Total Count
        const count = await Candle.countDocuments();
        console.log(`\n📊 Total Candles in DB: ${count}`);

        // 2. Check 1H Candles specifically
        const count1h = await Candle.countDocuments({ timeframe: '1h' });
        console.log(`📊 1H Candles found: ${count1h}`);

        // 3. Check the most recent candle
        const latest = await Candle.findOne({ timeframe: '1h' }).sort({ time: -1 });
        if (latest) {
            console.log("\n🔎 LATEST CANDLE DATA:");
            console.log(JSON.stringify(latest, null, 2));
        } else {
            console.log("\n❌ No 1H candles found! Check your Schema.");
        }

    } catch (err) {
        console.error("❌ Error:", err);
    } finally {
        process.exit();
    }
};

runDebug();