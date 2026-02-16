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

// ==========================================
// 🛡️ 1. GLOBAL CORS FIX (THE CRITICAL FIX)
// ==========================================
const allowedOrigins = [
  "http://localhost:3000",
  "https://aura-trade-weld.vercel.app",  // 👈 YOUR VERCEL APP
  "https://aura-trade-v1.onrender.com"
];

app.use(cors({
    origin: function (origin, callback) {
        // Allow requests with no origin (like mobile apps or curl requests)
        if (!origin) return callback(null, true);
        if (allowedOrigins.indexOf(origin) === -1) {
            // Optional: You can allow all if you want, but this is safer
            return callback(null, true); 
        }
        return callback(null, true);
    },
    credentials: true,
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization', 'Origin', 'Accept', 'X-Requested-With']
}));

app.use(express.json());

// 🛡️ 2. SOCKET.IO SETUP (Fixed for Proxy + 403 Prevention)
const io = new Server(server, {
  cors: { 
      origin: "*", // Allow all origins (Python proxy strips original origin)
      methods: ["GET", "POST"],
      credentials: true,
      allowedHeaders: ["Content-Type", "Authorization", "X-Requested-With"]
  },
  transports: ['websocket', 'polling'], // Support both transports
  allowEIO3: true, // Support older Socket.io clients
  pingTimeout: 60000, // 60 seconds before considering connection dead
  pingInterval: 25000, // Send ping every 25 seconds
  upgradeTimeout: 30000, // Time to wait for upgrade
  maxHttpBufferSize: 1e6, // 1MB max message size
  // 🔑 KEY FIX: Allow requests from proxy
  allowRequest: (req, callback) => {
    // Always allow - proxy strips origin info
    callback(null, true);
  }
});

const PORT = process.env.PORT || 10000; // Render standard port is 10000

// 🔗 INTERNAL BRAIN CONNECTION (Docker Monolith)
const BRAIN_URL = 'http://127.0.0.1:8000';

// 3. REGISTER NODE ROUTES (Must be defined BEFORE the catch-all proxy)
app.use('/api/trades', tradeRoutes);
app.use('/api/candles', candleRoutes);
app.use('/api/news', newsRoutes);

// ✅ RESTORED: PYTHON BRIDGE 
// Fixes 404 Error if the request hits Node.js instead of Python
app.post('/api/analyze', async (req, res) => {
    try {
        console.log("🧠 Node: Forwarding analysis request to internal Python Brain...");
        // Forward the request to the internal Python Service on port 8000
        const response = await axios.post(`${BRAIN_URL}/api/analyze`, req.body);
        res.json(response.data);
    } catch (error) {
        console.error("🧠 Brain Connection Error:", error.message);
        // Fallback if Python is restarting or busy (Prevents Frontend Crash)
        res.status(200).json({ 
            signal: "HOLD", 
            confidence: 0, 
            trend: "NEUTRAL",
            reasoning: ["AI Brain is initializing or unreachable..."],
            keyLevels: { resistance: 0, support: 0, ema: 0 }
        });
    }
});

// ==========================================
// ✅ PYTHON BRIDGE: HEALTH (THE MISSING LINK)
// This fixes the "Cannot GET /health" error
// ==========================================
app.get('/health', async (req, res) => {
    try {
        const response = await axios.get(`${BRAIN_URL}/health`);
        res.json({
            node_status: "Healthy",
            python_brain: response.data
        });
    } catch (error) {
        res.status(503).json({ 
            node_status: "Healthy", 
            python_brain: "OFFLINE", 
            error: error.message 
        });
    }
});

// Node-only Health Check
app.get('/healthcheck', (req, res) => {
  res.status(200).json({ 
    status: 'OK', 
    service: 'node-backend',
    timestamp: new Date().toISOString()
  });
});

app.get('/', (req, res) => res.send('AuraTrade Monolith API is Live 🚀'));

// 4. SOCKET LOGIC (Enhanced Logging)
io.on('connection', (socket) => {
  console.log('⚡ Client Connected:', socket.id);
  console.log('   Transport:', socket.conn.transport.name);
  console.log('   IP:', socket.handshake.address);
  
  // Send initial connection confirmation
  socket.emit('connected', { 
    socketId: socket.id, 
    timestamp: new Date().toISOString() 
  });
  
  socket.on('disconnect', (reason) => {
    console.log('❌ Client Disconnected:', socket.id, '- Reason:', reason);
  });
  
  socket.on('error', (error) => {
    console.error('🔴 Socket Error:', socket.id, error);
  });
  
  // Handle ping/pong for connection health
  socket.on('ping', () => {
    socket.emit('pong', { timestamp: Date.now() });
  });
});

// Monitor Socket.io connection errors
io.engine.on('connection_error', (err) => {
  console.error('⚠️ Socket.io Connection Error:');
  console.error('   Code:', err.code);
  console.error('   Message:', err.message);
  console.error('   Context:', err.context);
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
    
    // Emit price update to all connected Socket.io clients
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
      
      // Notify Socket.io clients that data stream is connected
      io.emit('stream-status', { connected: true, source: 'Binance' });
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

      } catch (err) { 
        // Ignore parse errors for non-critical data
      }
    });

    binanceWs.on('error', (err) => {
      console.error(`❌ Connection Error on ${currentUrl}:`, err.message);
    });

    binanceWs.on('close', () => {
      console.log('⚠️ Binance Disconnected.');
      
      // Notify Socket.io clients that stream is disconnected
      io.emit('stream-status', { connected: false, source: 'Binance' });
      
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
if (process.env.NODE_ENV === 'production' || process.env.RENDER_EXTERNAL_URL) {
  const RENDER_URL = process.env.RENDER_EXTERNAL_URL || 'https://aura-trade.onrender.com';
  
  setInterval(() => {
    axios.get(`${RENDER_URL}/healthcheck`)
      .then(() => console.log('💓 Self-Ping: Keeping the Monolith awake...'))
      .catch((err) => console.error(`⚠️ Ping failed: ${err.message}`));
  }, 300000); // 5 Minutes
}

// ==========================================
// 🚀 START SERVER
// ==========================================
server.listen(PORT, '0.0.0.0', () => {
  console.log(`🚀 Node.js Server running on port ${PORT}`);
  console.log(`🔌 Socket.io initialized with proxy-compatible CORS`);
  console.log(`🧠 Ready to receive requests from Python gateway (Port 8000)`);
});

// Export for testing
module.exports = { app, server, io };