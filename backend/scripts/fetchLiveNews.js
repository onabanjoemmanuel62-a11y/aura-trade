const axios = require('axios');
const xml2js = require('xml2js');
const mongoose = require('mongoose');
const dotenv = require('dotenv');
const path = require('path');
const NewsEvent = require('../models/NewsEvent'); 

// --- 🛠️ CONFIG & SETUP ---
if (require.main === module) {
    console.log('🔧 Running in Standalone Mode...');
    dotenv.config({ path: path.join(__dirname, '../.env') });

    if (!process.env.MONGO_URI) {
        console.error('❌ CRITICAL ERROR: process.env.MONGO_URI is undefined.');
        process.exit(1);
    }
    const connectDB = require('../config/db');
    connectDB();
}

const FEED_URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.xml';

// 🛡️ Helper: Generate ID
const generateHashId = (str) => {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash = hash & hash; 
    }
    return Math.abs(hash);
};

const fetchLiveNews = async () => {
    console.log('📡 Fetching Live ForexFactory Calendar...');

    try {
        // --- 🕵️ STEALTH MODE ---
        const response = await axios.get(FEED_URL, {
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
            }
        });

        const parser = new xml2js.Parser({ explicitArray: false });
        const result = await parser.parseStringPromise(response.data);

        if (!result || !result.weeklyevents || !result.weeklyevents.event) {
            console.log('⚠️ Feed format unexpected or empty.');
            return;
        }
        
        const events = result.weeklyevents.event;
        const eventList = Array.isArray(events) ? events : [events];
        let count = 0;
        let skipped = 0;

        // --- 🛡️ THE SMART FILTER STRATEGY ---
        for (const item of eventList) {
            try {
                // 1. FILTER: USD ONLY (Gold/Crypto is priced in USD)
                if (item.country !== 'USD') {
                    skipped++;
                    continue;
                }

                // 2. FILTER: IMPACT (High & Medium Only)
                if (item.impact !== 'High' && item.impact !== 'Medium') {
                    skipped++;
                    continue;
                }

                // --- DATE PARSING LOGIC ---
                // Format: "MM-DD-YYYY" and "3:30pm"
                const [monthStr, dayStr, yearStr] = item.date.split('-');
                const timeMatch = item.time.match(/(\d+):(\d+)(am|pm)/i);
                
                if (!timeMatch) continue; 

                let [_, hours, minutes, modifier] = timeMatch;
                let h = parseInt(hours);
                let m = parseInt(minutes);

                if (modifier.toLowerCase() === 'pm' && h < 12) h += 12;
                if (modifier.toLowerCase() === 'am' && h === 12) h = 0;

                const dateObj = new Date(parseInt(yearStr), parseInt(monthStr) - 1, parseInt(dayStr), h, m, 0);
                const timeInSeconds = Math.floor(dateObj.getTime() / 1000);

                if (isNaN(timeInSeconds)) continue;

                // 3. FILTER: TIME LOCK (Don't save ancient history)
                // We keep events from the last 24 hours + all future events
                // This prevents your DB from filling up with last week's news if the feed drifts
                const nowSeconds = Math.floor(Date.now() / 1000);
                const oneDayAgo = nowSeconds - (24 * 60 * 60);

                if (timeInSeconds < oneDayAgo) {
                    skipped++;
                    continue;
                }

                // --- SAVE TO DATABASE ---
                const uniqueSignature = `${item.country}-${item.title}-${timeInSeconds}`;
                const syntheticId = generateHashId(uniqueSignature);
                const parseNum = (val) => (val && val.trim() !== '' ? parseFloat(val) : null);

                const newsPayload = {
                    originalId: syntheticId,
                    time: timeInSeconds,
                    currency: item.country,
                    event: item.title,
                    impact: item.impact, 
                    forecast: parseNum(item.forecast),
                    previous: parseNum(item.previous),
                    actual: parseNum(item.actual) 
                };

                await NewsEvent.findOneAndUpdate(
                    { originalId: syntheticId },
                    { $set: newsPayload },
                    { upsert: true, new: true }
                );

                count++;
            } catch (err) {
                console.log(`⚠️ Parsing Error for ${item.date}:`, err.message);
            }
        }

        console.log(`✅ Smart Sync Complete. Saved ${count} USD Events. (Filtered out ${skipped} noise)`);

    } catch (error) {
        if (error.response && error.response.status === 429) {
            console.error('❌ Blocked (429). Please wait 5 minutes before running again.');
        } else {
            console.error('❌ Error fetching news feed:', error.message);
        }
    }
};

module.exports = fetchLiveNews;

if (require.main === module) {
    fetchLiveNews().then(() => {
        console.log('👋 Manual execution finished.');
        setTimeout(() => process.exit(), 1000);
    });
}