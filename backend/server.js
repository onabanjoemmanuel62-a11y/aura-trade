const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const WebSocket = require('ws');
const cors = require('cors');
const dotenv = require('dotenv');
const dns = require('dns');
const connectDB = require('./config/db');
const cron = require('node-cron');
const fetchLiveNews = require('./scripts/fetchLiveNews');
const axios = require('axios'); 

// 1. IMPORT THE MODEL
const Candle = require('./models/Candle');

// 2. IMPORT ROUTES (Note the destructuring for analysis)
const tradeRoutes = require('./routes/tradeRoutes');
const candleRoutes = require('./routes/candleRoutes');
const { router: analysisRoutes, analyzeMarket } = require('./routes/analysisRoutes'); // <--- UPDATED
const newsRoutes = require('./routes/newsRoutes');

// ISP Bypass: DNS Setup
try {
  dns.setServers(['8.8.8.8', '8.8.4.4', '1.1.1.1']);
  console.log('🌍 DNS configured to bypass ISP (Using Google 8.8.8.8)');
} catch (e) {
  console.error('⚠️ Could not set custom DNS:', e.message);
}

process.on('uncaughtException', (err) => {
  console.error('🔥 UNCAUGHT EXCEPTION! Keeping server alive...');
  console.error(err);
});

dotenv.config();
connectDB();

const app = express();
const server = http.createServer(app);

app.use(cors({ origin: "*" }));
app.use(express.json());

const io = new Server(server, {
  cors: { origin: "*", methods: ["GET", "POST"] }
});

// Make IO accessible globally for the analyzer if needed, 
// though we pass it directly in the logic below.
global.io = io; 

const PORT = process.env.PORT || 5000;

// 3. REGISTER ROUTES
app.use('/api/trades', tradeRoutes);
app.use('/api/candles', candleRoutes);
app.use('/api/analyze', analysisRoutes);
app.use('/api/news', newsRoutes);

app.get('/healthcheck', (req, res) => res.status(200).send('OK'));
app.get('/', (req, res) => res.send('API is Running'));

io.on('connection', (socket) => {
  console.log('⚡ Frontend Client Connected:', socket.id);
  socket.on('disconnect', () => console.log('❌ Frontend Client Disconnected:', socket.id));
});

// ==========================================
// 🕒 AUTOMATION & HELPERS
// ==========================================
fetchLiveNews();
cron.schedule('0 */6 * * *', () => {
  console.log('⏰ CRON: Starting scheduled news sync...');
  fetchLiveNews();
});

const isMarketClosed = (timestampInSeconds) => {
  const date = new Date(timestampInSeconds * 1000);
  const day = date.getUTCDay();    
  const hour = date.getUTCHours(); 
  if (day === 6) return true; 
  if (day === 5 && hour >= 22) return true; 
  if (day === 0 && hour < 22) return true; 
  return false; 
};

const getBucketTime = (timestamp, timeframe) => {
  if (timeframe === '1h') return timestamp - (timestamp % 3600);
  if (timeframe === '4h') return timestamp - (timestamp % 14400); 
  return timestamp;
};

// ==========================================
// 🛡️ CORE LOGIC: HANDLE NEW TICK & TRIGGER AI
// ==========================================
const handleNewTick = async (data) => {
  try {
    const price = data.close;
    const weekendFlag = isMarketClosed(data.time);
    const targetTimeframes = ['1h', '4h'];

    const updatePromises = targetTimeframes.map(async (tf) => {
      const bucketTime = getBucketTime(data.time, tf);
      const query = { time: bucketTime, timeframe: tf };

      const update = {
        $max: { high: data.high },
        $min: { low: data.low },
        $set: { close: data.close, isWeekend: weekendFlag },
        $setOnInsert: { open: data.open, time: bucketTime, timeframe: tf }
      };

      await Candle.findOneAndUpdate(query, update, { upsert: true, new: true });
      
      // 🧠 AI TRIGGER POINT
      // We only run the heavy AI logic on the 1H timeframe to save resources
      if (tf === '1h') {
          // console.log(`💾 [1H UPDATE] Bucket: ${bucketTime} | Price: ${price}`);
          
          // Trigger the Brain!
          try {
             const aiResult = await analyzeMarket(bucketTime, '1h');
             // Broadcast Prediction + Ghost Path
             io.emit('prediction-update', aiResult);
          } catch (aiErr) {
             console.error("⚠️ AI Brain Glitch:", aiErr.message);
          }
      }
    });

    await Promise.all(updatePromises);

  } catch (err) {
    console.error("❌ DB SAVE FAILED: ", err.message);
    throw err; 
  }
};

// ==========================================
// 🛡️ WEBSOCKET CONNECTION
// ==========================================
const BINANCE_ENDPOINTS = [
  'wss://stream.binance.com:443/ws/paxgusdt@kline_1m',
  'wss://data-stream.binance.com:443/ws/paxgusdt@kline_1m',
  'wss://data-stream.binance.vision:443/ws/paxgusdt@kline_1m',
];

let currentEndpointIndex = 0;
let failureCount = 0;
let binanceWs = null;

const connectBinanceStream = () => {
  const currentUrl = BINANCE_ENDPOINTS[currentEndpointIndex];
  
  try {
    console.log(`🔌 Connecting to Binance [Attempt ${failureCount + 1}]`);
    binanceWs = new WebSocket(currentUrl);

    binanceWs.on('open', () => {
      console.log('✅ Connected to Binance! Stream is live.');
      failureCount = 0;
    });

    binanceWs.on('message', async (data) => {
      try {
        const json = JSON.parse(data);
        const k = json.k;
        const timeInSeconds = Math.floor(k.t / 1000);
        const candlePayload = {
          time: timeInSeconds,
          open: parseFloat(k.o),
          high: parseFloat(k.h),
          low: parseFloat(k.l),
          close: parseFloat(k.c),
        };

        // 1. Save to DB & Run AI
        await handleNewTick(candlePayload);
        // 2. Send Live Price to Frontend
        io.emit('price-update', candlePayload);

      } catch (err) {
        if (err.message && !err.message.includes('DB SAVE FAILED')) { }
      }
    });

    binanceWs.on('error', (err) => console.error(`❌ WS Error:`, err.message));
    binanceWs.on('close', () => {
      console.log('⚠️ Binance Disconnected.');
      handleDisconnection();
    });

  } catch (error) {
    console.error('❌ Fatal Error creating WebSocket:', error.message);
    handleDisconnection();
  }
};

const handleDisconnection = () => {
  failureCount++;
  if (failureCount < 3) {
      setTimeout(connectBinanceStream, 3000);
      return;
  }
  currentEndpointIndex++;
  if (currentEndpointIndex >= BINANCE_ENDPOINTS.length) {
    currentEndpointIndex = 0;
  }
  failureCount = 0;
  setTimeout(connectBinanceStream, 2000);
};

connectBinanceStream();

// Keep Alive
if (process.env.NODE_ENV === 'production' || process.env.RENDER_EXTERNAL_URL) {
  const RENDER_URL = process.env.RENDER_EXTERNAL_URL || 'https://aura-trade.onrender.com';
  setInterval(() => {
    axios.get(`${RENDER_URL}/healthcheck`).catch((err) => console.error(`⚠️ Ping failed`));
  }, 300000);
}

server.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});