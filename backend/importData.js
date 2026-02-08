const mongoose = require('mongoose');
const dotenv = require('dotenv');
const xlsx = require('xlsx');
const connectDB = require('./config/db');
const Candle = require('./models/Candle');

dotenv.config();
connectDB();

const filename = process.argv[2];
const timeframe = process.argv[3];

if (!filename || !timeframe) {
  console.error('❌ Usage: node backend/importData.js <filename> <timeframe>');
  process.exit(1);
}

const importData = async () => {
  try {
    console.log(`📂 Reading ${filename}...`);
    const workbook = xlsx.readFile(filename, { cellDates: true });
    const sheet = workbook.Sheets[workbook.SheetNames[0]];
    const rawData = xlsx.utils.sheet_to_json(sheet);

    console.log(`📊 Found ${rawData.length} rows. Mapping data...`);

    const candles = rawData.map(row => {
      // Handle Date formats: Convert to Unix Timestamp (Seconds)
      let dateObj = row.Date || row.Time || row.date;
      if (!(dateObj instanceof Date)) dateObj = new Date(dateObj);
      
      return {
        time: Math.floor(dateObj.getTime() / 1000), // Convert ms to seconds
        open: row.Open || row.open,
        high: row.High || row.high,
        low: row.Low || row.low,
        close: row.Close || row.close,
        timeframe: timeframe
      };
    });

    // Clear old data for this timeframe only
    console.log(`🗑️  Clearing old ${timeframe} data...`);
    await Candle.deleteMany({ timeframe });

    // BATCH INSERT (Chunk size: 1000)
    const BATCH_SIZE = 1000;
    console.log(`🚀 Starting Batch Insert (Size: ${BATCH_SIZE})...`);
    
    for (let i = 0; i < candles.length; i += BATCH_SIZE) {
      const chunk = candles.slice(i, i + BATCH_SIZE);
      await Candle.insertMany(chunk);
      process.stdout.write(`.`); // Progress bar effect
    }

    console.log(`\n✅ Success! Imported ${candles.length} candles.`);
    process.exit();
  } catch (error) {
    console.error(`❌ Error: ${error.message}`);
    process.exit(1);
  }
};

importData();