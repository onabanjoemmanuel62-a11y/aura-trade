const mongoose = require('mongoose');

const connectDB = async () => {
  try {
    // --- X-RAY DEBUGGING START ---
    const uri = process.env.MONGO_URI;
    console.log(`---------------------------------------------------`);
    console.log(`DEBUG: Type is [${typeof uri}]`);
    console.log(`DEBUG: Value is |${uri}|`); 
    console.log(`---------------------------------------------------`);
    // --- X-RAY DEBUGGING END ---

    const conn = await mongoose.connect(uri);

    console.log(`MongoDB Connected: ${conn.connection.host}`);
  } catch (error) {
    console.error(`Error: ${error.message}`);
    process.exit(1);
  }
};

module.exports = connectDB;