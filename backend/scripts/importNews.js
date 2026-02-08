const fs = require('fs');
const mongoose = require('mongoose');
const csv = require('csv-parser');
const path = require('path');
const dotenv = require('dotenv');

// ==========================================
// 🔧 1. PATH CONFIGURATION (Fixed)
// ==========================================

// We are in: backend/scripts/
// We need:   backend/.env
const envPath = path.join(__dirname, '../.env'); 
console.log(`🔍 Loading .env from: ${envPath}`);

const result = dotenv.config({ path: envPath });

if (result.error) {
  console.error("❌ ERROR: Could not find .env file!");
  process.exit(1);
}

// Verify DB Connection String
if (!process.env.MONGO_URI) {
  console.error("❌ ERROR: .env loaded, but MONGO_URI is missing.");
  process.exit(1);
}

const NewsEvent = require('../models/NewsEvent');
const connectDB = require('../config/db');

// ==========================================
// 🚀 2. MAIN IMPORT LOGIC
// ==========================================

const importData = async () => {
  await connectDB();

  const results = [];
  // We are in: backend/scripts/
  // We need:   backend/scrape.csv
  const csvFilePath = path.join(__dirname, '../scrape.csv');
  
  console.log(`📂 Reading CSV from: ${csvFilePath}`);

  if (!fs.existsSync(csvFilePath)) {
    console.error("❌ ERROR: scrape.csv not found!");
    process.exit(1);
  }

  fs.createReadStream(csvFilePath)
    .pipe(csv())
    .on('data', (data) => {
      const timeInSeconds = Math.floor(new Date(data.datetime).getTime() / 1000);
      
      const parseNum = (val) => (val && val.trim() !== '' ? parseFloat(val) : null);

      if (!isNaN(timeInSeconds)) {
        results.push({
          updateOne: {
            filter: { originalId: parseInt(data.id) },
            update: {
              $set: {
                originalId: parseInt(data.id),
                time: timeInSeconds,
                currency: data.currency,
                event: data.event,
                impact: data.impact,
                actual: parseNum(data.actual),
                forecast: parseNum(data.forecast),
                previous: parseNum(data.previous)
              }
            },
            upsert: true
          }
        });
      }
    })
    .on('end', async () => {
      console.log(`📊 Parsed ${results.length} news events.`);
      console.log('💾 Starting Bulk Write to MongoDB...');

      try {
        const BATCH_SIZE = 5000;
        for (let i = 0; i < results.length; i += BATCH_SIZE) {
          const batch = results.slice(i, i + BATCH_SIZE);
          await NewsEvent.bulkWrite(batch);
          process.stdout.write(`.`); // Visual progress dot
        }

        console.log('\n🎉 SUCCESS: Import Finished!');
        process.exit();
      } catch (err) {
        console.error('\n❌ DB Write Failed:', err);
        process.exit(1);
      }
    });
};

importData();