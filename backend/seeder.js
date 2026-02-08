const mongoose = require('mongoose');
const dotenv = require('dotenv');
const Trade = require('./models/Trade');
const connectDB = require('./config/db');

// 1. Load Config
dotenv.config();
connectDB();

// 2. The Dummy Data
const sampleTrades = [
  { pair: 'XAUUSD', action: 'Buy', entry: 2020.50, exit: 2025.50, result: 'WON', profit: 500, timestamp: new Date('2023-10-01') },
  { pair: 'XAUUSD', action: 'Sell', entry: 2030.00, exit: 2035.00, result: 'LOST', profit: -500, timestamp: new Date('2023-10-02') },
  { pair: 'XAUUSD', action: 'Buy', entry: 2015.00, exit: 2020.00, result: 'WON', profit: 500, timestamp: new Date('2023-10-03') },
  { pair: 'XAUUSD', action: 'Sell', entry: 2040.00, exit: 2038.00, result: 'WON', profit: 200, timestamp: new Date('2023-10-04') },
  { pair: 'XAUUSD', action: 'Buy', entry: 2022.00, exit: 2018.00, result: 'LOST', profit: -400, timestamp: new Date('2023-10-05') },
  { pair: 'XAUUSD', action: 'Buy', entry: 2025.00, exit: 2035.00, result: 'WON', profit: 1000, timestamp: new Date('2023-10-06') },
  { pair: 'XAUUSD', action: 'Sell', entry: 2050.00, exit: 2055.00, result: 'LOST', profit: -500, timestamp: new Date('2023-10-07') },
  { pair: 'XAUUSD', action: 'Buy', entry: 2010.00, exit: 2015.00, result: 'WON', profit: 500, timestamp: new Date('2023-10-08') },
  { pair: 'XAUUSD', action: 'Sell', entry: 2060.00, exit: 2050.00, result: 'WON', profit: 1000, timestamp: new Date('2023-10-09') },
  { pair: 'XAUUSD', action: 'Buy', entry: 2030.00, exit: 2028.00, result: 'LOST', profit: -200, timestamp: new Date('2023-10-10') },
];

// 3. The Import Function
const importData = async () => {
  try {
    // Clear existing data first so we don't get duplicates
    await Trade.deleteMany();

    // Insert new data
    await Trade.insertMany(sampleTrades);

    console.log('✅ Data Imported Successfully!');
    process.exit();
  } catch (error) {
    console.error(`❌ Error: ${error.message}`);
    process.exit(1);
  }
};

importData();