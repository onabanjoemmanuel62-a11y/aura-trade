const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const cors = require('cors');
const dotenv = require('dotenv');
const dns = require('dns');
const connectDB = require('./config/db');
const cron = require('node-cron');
const fetchLiveNews = require('./scripts/fetchLiveNews');
const axios = require('axios'); 
const yahooFinance = require('yahoo-finance2').default; // 👈 ADDED YAHOO FINANCE

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
// 🛡️ 1. GLOBAL CORS
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
      credentials: true
  },
  transports: ['websocket', 'polling'], 
  allowEIO3: true, 
  pingTimeout: 60000, 
  pingInterval: 25000, 
  allowRequest: (req, callback) => { callback(null, true); }
});

const PORT = process.env.PORT || 10000; 
const BRAIN_URL = 'http://127.0.0.1:8000';

// 3. REGISTER NODE ROUTES
app.use('/api/trades', tradeRoutes);
app.use('/api/candles', candleRoutes);
app.use('/api/news', newsRoutes);

// ✅ PYTHON BRIDGE 
app.post('/api/analyze', async (req, res) => {
    try {
        console.log("🧠 Node: Forwarding analysis request to internal Python Brain...");
        const response = await axios.post(`${BRAIN_URL}/api/analyze`, req.body);
        res.json(response.data);
    } catch (error) {
        console.error("🧠 Brain Connection Error:", error.message);
        res.status(200).json({ 
            signal: "HOLD", confidence: 0, trend: "NEUTRAL",
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

app.get('/', (req, res) => res.send('AuraTrade Monolith API is Live 🚀'));

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
// 🛡️ REPLACED BINANCE WITH YAHOO FINANCE
// ==========================================
const startLivePriceFeed = () => {
    console.log('🔌 Starting Live Yahoo Finance Feed (GC=F)...');
    
    setInterval(async () => {
        try {
            // Fetch 1-minute chart data to get the very latest price
            const result = await yahooFinance.chart('GC=F', { interval: '1m', range: '1d' });
            
            if (result && result.quotes && result.quotes.length > 0) {
                const latest = result.quotes[result.quotes.length - 1];
                if (!latest.close) return; // Skip if market is closed/empty

                const timeInSeconds = Math.floor(new Date(latest.date).getTime() / 1000);
                
                const candlePayload = {
                    time: timeInSeconds,
                    open: latest.open,
                    high: latest.high,
                    low: latest.low,
                    close: latest.close,
                };

                await handleNewTick(candlePayload);
            }
        } catch (error) {
            console.error('❌ Yahoo Live Feed Error:', error.message);
        }
    }, 60000); // Poll every 60 seconds
};

startLivePriceFeed();

// ==========================================
// 💓 KEEP ALIVE PING
// ==========================================
if (process.env.NODE_ENV === 'production' || process.env.RENDER_EXTERNAL_URL) {
  const RENDER_URL = process.env.RENDER_EXTERNAL_URL || 'https://aura-trade.onrender.com';
  setInterval(() => {
    axios.get(`${RENDER_URL}/healthcheck`)
      .then(() => console.log('💓 Self-Ping: Keeping the Monolith awake...'))
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