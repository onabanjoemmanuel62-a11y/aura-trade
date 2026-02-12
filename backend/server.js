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

// 2. IMPORT ROUTES
const tradeRoutes = require('./routes/tradeRoutes');
const candleRoutes = require('./routes/candleRoutes');
// const analysisRoutes = require('./routes/analysisRoutes'); // ❌ REMOVED: Old Logic
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

// 🛡️ UPDATED CORS: Fix for "Network Error" / Access-Control-Allow-Origin
app.use(cors({
    origin: true, // 👈 ALLOW ALL ORIGINS (Vercel, Localhost, etc.)
    credentials: true,
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization', 'Origin', 'Accept']
}));

// Enable Pre-flight checks for all routes
app.options('*', cors());

app.use(express.json());

const io = new Server(server, {
  cors: { 
      origin: "*", 
      methods: ["GET", "POST"],
      credentials: true 
  }
});

const PORT = process.env.PORT || 5000;

// 🔗 INTERNAL BRAIN CONNECTION (Docker Monolith)
// Node talks to Python via Localhost on port 8000
const BRAIN_URL = 'http://127.0.0.1:8000';

// 3. REGISTER ROUTES
app.use('/api/trades', tradeRoutes);
app.use('/api/candles', candleRoutes);
app.use('/api/news', newsRoutes);

// ✅ NEW: Proxy Request to Python Brain
app.post('/api/analyze', async (req, res) => {
    try {
        // Forward the request to the internal Python Service
        const response = await axios.post(`${BRAIN_URL}/api/analyze`, req.body);
        res.json(response.data);
    } catch (error) {
        console.error("🧠 Brain Error:", error.message);
        // Fallback if Python is restarting or busy
        res.status(500).json({ 
            signal: "HOLD", 
            confidence: 0, 
            trend: "NEUTRAL",
            reasoning: ["AI Brain is initializing..."] 
        });
    }
});

// Health Check Route
app.get('/healthcheck', (req, res) => res.status(200).send('OK'));

app.get('/', (req, res) => res.send('AuraTrade API is Running'));

io.on('connection', (socket) => {
  console.log('⚡ Frontend Client Connected:', socket.id);
  socket.on('disconnect', () => console.log('❌ Frontend Client Disconnected:', socket.id));
});

// ==========================================
// 🕒 AUTOMATION: DATA SYNC
// ==========================================

// 1. Run immediately on server start
fetchLiveNews();

// 2. Schedule: Run every 6 hours
cron.schedule('0 */6 * * *', () => {
  console.log('⏰ CRON: Starting scheduled news sync...');
  fetchLiveNews();
});

// ==========================================
// 🛡️ HELPER: WEEKEND DETECTOR
// ==========================================
const isMarketClosed = (timestampInSeconds) => {
    const date = new Date(timestampInSeconds * 1000);
    const day = date.getUTCDay(); // 0 = Sunday, 6 = Saturday
    const hour = date.getUTCHours(); 
  
    if (day === 6) return true; 
    if (day === 5 && hour >= 22) return true; 
    if (day === 0 && hour < 22) return true; 
  
    return false; 
};

// ==========================================
// 🛡️ HELPER: BUCKET CALCULATOR
// ==========================================
const getBucketTime = (timestamp, timeframe) => {
  if (timeframe === '1h') return timestamp - (timestamp % 3600);
  if (timeframe === '4h') return timestamp - (timestamp % 14400); 
  return timestamp;
};

// ==========================================
// 🛡️ CORE LOGIC: HANDLE NEW TICK
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
        $set: { 
          close: data.close,
          isWeekend: weekendFlag 
        },
        $setOnInsert: { 
          open: data.open, 
          time: bucketTime, 
          timeframe: tf 
        }
      };

      await Candle.findOneAndUpdate(query, update, { upsert: true, new: true });
    });

    await Promise.all(updatePromises);
    io.emit('price-update', data);

  } catch (err) {
    console.error("❌ DB SAVE FAILED: ", err.message);
  }
};

// ==========================================
// 🛡️ ISP BYPASS & CONNECTION LOGIC
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
  if (currentEndpointIndex >= BINANCE_ENDPOINTS.length) currentEndpointIndex = 0;
  const currentUrl = BINANCE_ENDPOINTS[currentEndpointIndex];
  
  try {
    console.log(`🔌 Connecting to Binance [Attempt ${failureCount + 1}]`);
    console.log(`👉 Target: ${currentUrl}`);

    binanceWs = new WebSocket(currentUrl);

    binanceWs.on('open', () => {
      console.log('✅ Connected to Binance! Stream is live.');
      failureCount = 0;
    });

    binanceWs.on('message', async (raw) => {
      try {
        const json = JSON.parse(raw);
        const k = json.k;
        const timeInSeconds = Math.floor(k.t / 1000);

        const candlePayload = {
          time: timeInSeconds,
          open: parseFloat(k.o),
          high: parseFloat(k.h),
          low: parseFloat(k.l),
          close: parseFloat(k.c),
        };

        await handleNewTick(candlePayload);

      } catch (err) { }
    });

    binanceWs.on('error', (err) => {
      console.error(`❌ Connection Error on ${currentUrl}:`, err.message);
    });

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
      console.log(`⏳ Retrying same endpoint in 3s... (${failureCount}/3)`);
      setTimeout(connectBinanceStream, 3000);
      return;
  }
  console.log(`🚫 Endpoint ${BINANCE_ENDPOINTS[currentEndpointIndex]} seems blocked.`);
  currentEndpointIndex++;
  console.log(`🔀 Switching to fallback...`);
  failureCount = 0;
  setTimeout(connectBinanceStream, 2000);
};

connectBinanceStream();

// ==========================================
// 💓 KEEP ALIVE PING (Monolith Edition)
// ==========================================
// Since Node and Python are in the same container, 
// keeping Node awake keeps Python awake too.
if (process.env.NODE_ENV === 'production' || process.env.RENDER_EXTERNAL_URL) {
  const RENDER_URL = process.env.RENDER_EXTERNAL_URL || 'https://aura-trade.onrender.com';
  
  setInterval(() => {
    axios.get(`${RENDER_URL}/healthcheck`)
      .then(() => console.log('💓 Self-Ping: Keeping the Monolith awake...'))
      .catch((err) => console.error(`⚠️ Ping failed: ${err.message}`));
  }, 300000); // 5 Minutes
}

server.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});