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

// ✅ Correctly instantiate Yahoo Finance
const YahooFinance = require('yahoo-finance2').default;
const yahooFinance = new YahooFinance(); 

const Candle = require('./models/Candle');
const tradeRoutes = require('./routes/tradeRoutes');
const candleRoutes = require('./routes/candleRoutes');
const newsRoutes = require('./routes/newsRoutes');

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

// 🏆 THE SQUAD: Gold + 7 Forex Majors (Yahoo Finance Tickers)
const ASSETS = [
  'GC=F',      // Gold
  'EURUSD=X',  // EUR/USD
  'GBPUSD=X',  // GBP/USD
  'JPY=X',     // USD/JPY (Yahoo uses JPY=X)
  'AUDUSD=X',  // AUD/USD
  'CAD=X',     // USD/CAD (Yahoo uses CAD=X)
  'CHF=X',     // USD/CHF (Yahoo uses CHF=X)
  'NZDUSD=X'   // NZD/USD
];

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

io.on('connection', (socket) => {
  console.log('⚡ Client Connected:', socket.id);
  socket.emit('connected', { socketId: socket.id, timestamp: new Date().toISOString() });
  socket.on('disconnect', () => console.log('❌ Client Disconnected:', socket.id));
});

// ==========================================
// 🛡️ HELPERS
// ==========================================
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

// ✅ FIX: Added `symbol` parameter to handle multi-asset routing
const handleNewTick = async (data, symbol) => {
  try {
    const weekendFlag = isMarketClosed(data.time);
    const targetTimeframes = ['1h', '4h'];

    const updatePromises = targetTimeframes.map(async (tf) => {
      const bucketTime = getBucketTime(data.time, tf);
      // ✅ FIX: Query includes the symbol
      const query = { symbol: symbol, time: bucketTime, timeframe: tf };
      const update = {
        $max: { high: data.high },
        $min: { low: data.low },
        $set: { close: data.close, isWeekend: weekendFlag },
        // ✅ FIX: Insert includes the symbol
        $setOnInsert: { symbol: symbol, open: data.open, time: bucketTime, timeframe: tf }
      };
      await Candle.findOneAndUpdate(query, update, { upsert: true, new: true });
    });

    await Promise.all(updatePromises);
    
    // ✅ FIX: Emit the symbol so the frontend knows who scored the update
    io.emit('price-update', { symbol, ...data });

  } catch (err) {
    console.error(`❌ DB SAVE FAILED for ${symbol}: `, err.message);
  }
};

// ==========================================
// 🔄 AUTO-CATCH-UP (Fills gaps when server was off)
// ==========================================
const syncHistoricalData = async () => {
    console.log("🔄 AUTO-CATCH-UP: Patching missing candles for ALL assets...");
    try {
        const startDate = new Date();
        startDate.setDate(startDate.getDate() - 7);
        const period1 = startDate.toISOString().split('T')[0];

        // ✅ FIX: Loop through the entire squad
        for (const symbol of ASSETS) {
            try {
                const result = await yahooFinance.chart(symbol, { period1, interval: '1h' });
                const rawData = result?.quotes || [];

                if (rawData.length === 0) continue;

                for (const tf of ['1h', '4h']) {
                    const bulkOps = rawData.map(candle => {
                        if (!candle.date || !candle.close) return null;
                        const timeInSeconds = Math.floor(new Date(candle.date).getTime() / 1000);
                        const bucketTime = getBucketTime(timeInSeconds, tf);

                        return {
                            updateOne: {
                                // ✅ FIX: Added symbol to DB filter
                                filter: { symbol: symbol, time: bucketTime, timeframe: tf },
                                update: {
                                    $max: { high: candle.high },
                                    $min: { low: candle.low },
                                    $set: { close: candle.close, isWeekend: isMarketClosed(timeInSeconds) },
                                    // ✅ FIX: Added symbol to insert payload
                                    $setOnInsert: { symbol: symbol, open: candle.open, time: bucketTime, timeframe: tf }
                                },
                                upsert: true
                            }
                        };
                    }).filter(Boolean);

                    if (bulkOps.length > 0) {
                        await Candle.bulkWrite(bulkOps);
                    }
                }
                console.log(`✅ AUTO-CATCH-UP Complete for ${symbol}.`);
            } catch (err) {
                console.error(`⚠️ Catch-Up Failed for ${symbol}:`, err.message);
            }
        }
    } catch (err) {
        console.error("❌ Catch-Up Process Failed:", err.message);
    }
};

// ==========================================
// 🛡️ LIVE PRICE FEED (Yahoo Finance Polling)
// ==========================================
const startLivePriceFeed = () => {
    console.log(`🔌 Starting Live Yahoo Finance Feed for 8 Assets...`);
    
    setInterval(async () => {
        const startDate = new Date();
        startDate.setDate(startDate.getDate() - 1);
        const period1 = startDate.toISOString().split('T')[0];

        // ✅ FIX: Map through the assets and fetch them concurrently but safely
        const fetchPromises = ASSETS.map(async (symbol) => {
            try {
                const result = await yahooFinance.chart(symbol, { period1, interval: '1m' });
                
                if (result && result.quotes && result.quotes.length > 0) {
                    const latest = result.quotes[result.quotes.length - 1];
                    if (!latest.close) return; 

                    const timeInSeconds = Math.floor(new Date(latest.date).getTime() / 1000);
                    
                    const candlePayload = {
                        time: timeInSeconds,
                        open: latest.open,
                        high: latest.high,
                        low: latest.low,
                        close: latest.close,
                    };

                    // ✅ FIX: Pass the symbol to the handler
                    await handleNewTick(candlePayload, symbol);
                }
            } catch (error) {
                console.error(`⚠️ Yahoo Feed slight delay for ${symbol}...`);
            }
        });

        // Promise.allSettled ensures one failed ticker doesn't crash the loop for the others
        await Promise.allSettled(fetchPromises);

    }, 30000); // 30 second updates
};

// ==========================================
// 🕒 STARTUP & AUTOMATION RUNNERS
// ==========================================
const initializeDataEngines = async () => {
    await syncHistoricalData(); // 1. Patch holes for all pairs
    startLivePriceFeed();       // 2. Start real-time feed for all pairs
    fetchLiveNews();            // 3. Fetch initial news
};

initializeDataEngines();

cron.schedule('0 */4 * * *', () => syncHistoricalData());
cron.schedule('0 */6 * * *', () => fetchLiveNews());     

// ==========================================
// 💓 KEEP ALIVE PING
// ==========================================
if (process.env.NODE_ENV === 'production' || process.env.RENDER_EXTERNAL_URL) {
  const RENDER_URL = process.env.RENDER_EXTERNAL_URL || 'https://aura-trade.onrender.com';
  setInterval(() => {
    axios.get(`${RENDER_URL}/healthcheck`)
      .catch(() => {});
  }, 300000); 
}

// ==========================================
// 🚀 START SERVER
// ==========================================
server.listen(PORT, '0.0.0.0', () => {
  console.log(`🚀 Node.js Server running on port ${PORT}`);
  console.log(`🔌 Socket.io initialized`);
  console.log(`🧠 Ready to receive requests from Python gateway`);
});

module.exports = { app, server, io };