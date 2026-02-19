const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const WebSocket = require('ws'); // 👈 RESTORED FOR LIVE TICKS
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
const newsRoutes = require('./routes/newsRoutes');

// ISP Bypass: DNS Setup
try {
  dns.setServers(['8.8.8.8', '8.8.4.4', '1.1.1.1']);
  console.log('🌍 DNS configured to bypass ISP');
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

// ==========================================
// 🛡️ 1. GLOBAL CORS FIX
// ==========================================
const allowedOrigins = [
  "http://localhost:3000",
  "https://aura-trade-weld.vercel.app",  
  "https://aura-trade-v1.onrender.com"
];

app.use(cors({
    origin: function (origin, callback) {
        if (!origin) return callback(null, true);
        if (allowedOrigins.indexOf(origin) === -1) {
            return callback(null, true); 
        }
        return callback(null, true);
    },
    credentials: true,
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization', 'Origin', 'Accept', 'X-Requested-With']
}));

app.use(express.json());

// 🛡️ 2. SOCKET.IO SETUP
const io = new Server(server, {
  cors: { 
      origin: "*", 
      methods: ["GET", "POST"],
      credentials: true,
      allowedHeaders: ["Content-Type", "Authorization", "X-Requested-With"]
  },
  transports: ['websocket', 'polling'], 
  allowEIO3: true, 
  pingTimeout: 60000, 
  pingInterval: 25000, 
  upgradeTimeout: 30000, 
  maxHttpBufferSize: 1e6, 
  allowRequest: (req, callback) => {
    callback(null, true);
  }
});

const PORT = process.env.PORT || 10000; 
const BRAIN_URL = 'http://127.0.0.1:8000';

// 3. REGISTER NODE ROUTES
app.use('/api/trades', tradeRoutes);
app.use('/api/candles', candleRoutes);
app.use('/api/news', newsRoutes);

// ✅ RESTORED: PYTHON BRIDGE 
app.post('/api/analyze', async (req, res) => {
    try {
        console.log("🧠 Node: Forwarding analysis request to internal Python Brain...");
        const response = await axios.post(`${BRAIN_URL}/api/analyze`, req.body);
        res.json(response.data);
    } catch (error) {
        console.error("🧠 Brain Connection Error:", error.message);
        res.status(200).json({ 
            signal: "HOLD", 
            confidence: 0, 
            trend: "NEUTRAL",
            reason: ["AI Brain is initializing or unreachable..."],
            keyLevels: { resistance: 0, support: 0, ema: 0 }
        });
    }
});

app.get('/health', async (req, res) => {
    try {
        const response = await axios.get(`${BRAIN_URL}/health`);
        res.json({ node_status: "Healthy", python_brain: response.data });
    } catch (error) {
        res.status(503).json({ node_status: "Healthy", python_brain: "OFFLINE", error: error.message });
    }
});

app.get('/healthcheck', (req, res) => {
  res.status(200).json({ status: 'OK', service: 'node-backend', timestamp: new Date().toISOString() });
});

app.get('/', (req, res) => res.send('AuraTrade API is Live 🚀'));

// 4. SOCKET LOGIC
io.on('connection', (socket) => {
  console.log('⚡ Client Connected:', socket.id);
  socket.emit('connected', { socketId: socket.id, timestamp: new Date().toISOString() });
  socket.on('disconnect', (reason) => console.log('❌ Client Disconnected:', socket.id));
  socket.on('error', (error) => console.error('🔴 Socket Error:', socket.id, error));
});

// ==========================================
// 🕒 AUTOMATION: DATA SYNC
// ==========================================
fetchLiveNews();
cron.schedule('0 */6 * * *', () => fetchLiveNews());

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

const handleNewTick = async (data) => {
  try {
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
    });

    await Promise.all(updatePromises);
    io.emit('price-update', data);

  } catch (err) {
    console.error("❌ DB SAVE FAILED: ", err.message);
  }
};

// ==========================================
// ⚡ LIVE TICK FEED (RESTORED BINANCE WEBSOCKET)
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
    console.log(`🔌 Connecting to Binance WebSocket for LIVE Ticks...`);

    binanceWs = new WebSocket(currentUrl);

    binanceWs.on('open', () => {
      console.log('✅ Connected to Binance! Stream is live.');
      failureCount = 0;
      io.emit('stream-status', { connected: true, source: 'Binance' });
    });

    binanceWs.on('message', async (raw) => {
      try {
        const json = JSON.parse(raw);
        if (!json.k) return;
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

    binanceWs.on('error', (err) => console.error(`❌ WS Error:`, err.message));

    binanceWs.on('close', () => {
      console.log('⚠️ Binance Disconnected.');
      io.emit('stream-status', { connected: false, source: 'Binance' });
      handleDisconnection();
    });

  } catch (error) {
    console.error('❌ Fatal WS Error:', error.message);
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
  failureCount = 0;
  setTimeout(connectBinanceStream, 2000);
};

connectBinanceStream();

// ==========================================
// 💓 KEEP ALIVE PING
// ==========================================
if (process.env.NODE_ENV === 'production' || process.env.RENDER_EXTERNAL_URL) {
  const RENDER_URL = process.env.RENDER_EXTERNAL_URL || 'https://aura-trade.onrender.com';
  setInterval(() => {
    axios.get(`${RENDER_URL}/healthcheck`)
      .then(() => console.log('💓 Self-Ping...'))
      .catch((err) => console.error(`⚠️ Ping failed: ${err.message}`));
  }, 300000);
}

// ==========================================
// 🚀 START SERVER
// ==========================================
server.listen(PORT, '0.0.0.0', () => {
  console.log(`🚀 Node.js Server running on port ${PORT}`);
  console.log(`🔌 Socket.io initialized`);
  console.log(`🧠 Ready to receive requests from Python gateway (Port 8000)`);
});

module.exports = { app, server, io };