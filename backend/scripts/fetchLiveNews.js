const axios = require('axios');
const cheerio = require('cheerio');
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

// The XML feed (nfs.faireconomy.media/ff_calendar_thisweek.xml) never carries
// actual/outcome values — confirmed by inspecting the feed directly, every
// single <event> tag lacks an <actual> field entirely, even for events days
// in the past. The calendar WEBPAGE does show real actual values once
// released, so this scrapes that page's HTML table instead.
const CALENDAR_URL = 'https://www.forexfactory.com/calendar?week=this';

// 🛡️ Helper: Generate ID — same approach as the old scraper, so re-running
// this against the same event (same country+title+time) upserts the
// existing record rather than creating a duplicate.
const generateHashId = (str) => {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash = hash & hash;
    }
    return Math.abs(hash);
};

const parseNum = (val) => {
    if (!val) return null;
    const cleaned = val.trim().replace(/[%KMBT]/gi, '').replace(/,/g, '');
    if (cleaned === '' || cleaned === '-') return null;
    const num = parseFloat(cleaned);
    return isNaN(num) ? null : num;
};

// Country flag/name -> currency code. ForexFactory's calendar table shows
// the currency directly (USD, EUR, GBP, etc.) as the row's "currency" cell,
// so this is mostly a passthrough — kept as a map in case some rows show
// a country name instead in edge cases.
const CURRENCY_MAP = {
    USD: 'USD', EUR: 'EUR', GBP: 'GBP', JPY: 'JPY', AUD: 'AUD',
    NZD: 'NZD', CAD: 'CAD', CHF: 'CHF', CNY: 'CNY',
};

const fetchLiveNews = async () => {
    console.log('📡 Fetching Live ForexFactory Calendar (webpage scrape)...');

    try {
        const response = await axios.get(CALENDAR_URL, {
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            },
            timeout: 15000,
        });

        const $ = cheerio.load(response.data);
        const rows = $('tr.calendar__row');

        if (rows.length === 0) {
            console.log('⚠️ No calendar rows found — the page likely needs JS rendering (Puppeteer) rather than a static HTML fetch.');
            console.log('   Saving the raw response length for debugging:', response.data.length, 'chars.');
            return;
        }

        let currentDateLabel = null;
        let count = 0;
        let skipped = 0;

        rows.each((_, el) => {
            const $row = $(el);

            // Each day starts a new row with a date label; subsequent rows
            // for that day leave the date cell blank, so carry it forward.
            const dateCell = $row.find('.calendar__date').text().trim();
            if (dateCell) currentDateLabel = dateCell;
            if (!currentDateLabel) { skipped++; return; }

            const currency = $row.find('.calendar__currency').text().trim();
            if (!currency || !CURRENCY_MAP[currency]) { skipped++; return; }

            const impactTitle = $row.find('.calendar__impact span').attr('title') || '';
            let impact = 'Low';
            if (/High/i.test(impactTitle)) impact = 'High';
            else if (/Medium|Med /i.test(impactTitle)) impact = 'Medium';
            else if (/Non-Economic/i.test(impactTitle)) impact = 'Non-Economic';

            const event = $row.find('.calendar__event').text().trim();
            if (!event) { skipped++; return; }

            const timeText = $row.find('.calendar__time').text().trim();
            const actualText = $row.find('.calendar__actual').text().trim();
            const forecastText = $row.find('.calendar__forecast').text().trim();
            const previousText = $row.find('.calendar__previous').text().trim();

            // Date parsing: currentDateLabel is like "Mon Aug 3", timeText is
            // like "10:00am" (or "All Day" / "Tentative" for non-timed events).
            const timeMatch = timeText.match(/(\d+):(\d+)(am|pm)/i);
            const now = new Date();
            const yearGuess = now.getFullYear();
            const parsedDate = new Date(`${currentDateLabel} ${yearGuess}`);
            if (isNaN(parsedDate.getTime())) { skipped++; return; }

            // If the guessed date is more than ~6 months in the past, the
            // calendar has likely rolled into a new year — bump forward.
            if (parsedDate.getTime() < now.getTime() - (180 * 24 * 60 * 60 * 1000)) {
                parsedDate.setFullYear(yearGuess + 1);
            }

            if (timeMatch) {
                let [, hours, minutes, modifier] = timeMatch;
                let h = parseInt(hours);
                const m = parseInt(minutes);
                if (modifier.toLowerCase() === 'pm' && h < 12) h += 12;
                if (modifier.toLowerCase() === 'am' && h === 12) h = 0;
                parsedDate.setHours(h, m, 0, 0);
            } else {
                parsedDate.setHours(0, 0, 0, 0);
            }

            const timeInSeconds = Math.floor(parsedDate.getTime() / 1000);
            if (isNaN(timeInSeconds)) { skipped++; return; }

            const uniqueSignature = `${currency}-${event}-${currentDateLabel}`;
            const syntheticId = generateHashId(uniqueSignature);

            const newsPayload = {
                originalId: syntheticId,
                time: timeInSeconds,
                currency,
                event,
                impact,
                actual: parseNum(actualText),
                forecast: parseNum(forecastText),
                previous: parseNum(previousText),
            };

            NewsEvent.findOneAndUpdate(
                { originalId: syntheticId },
                { $set: newsPayload },
                { upsert: true, new: true }
            ).catch(err => console.log(`⚠️ DB write error for ${event}:`, err.message));

            count++;
        });

        console.log(`✅ Scrape complete. Processed ${count} events (skipped ${skipped} non-currency/header rows).`);
    } catch (error) {
        if (error.response && error.response.status === 429) {
            console.error('❌ Blocked (429). Please wait before running again.');
        } else if (error.response && error.response.status === 403) {
            console.error('❌ Blocked (403) — ForexFactory may be detecting this as a bot. May need different headers or Puppeteer.');
        } else {
            console.error('❌ Error scraping calendar:', error.message);
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