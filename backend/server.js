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

// 👈 CRITICAL FIX: Import the midfield controller that gathers candle data
const analysisController = require('./controllers/analysisController'); 

// 🤖 NEW: Import the Telegram Sniper Bot
const { startTelegramBot } = require('./scripts/telegramBot');

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

// 🏆 THE SQUAD: Gold + 7 Forex Majors (Yahoo Finance Tickers)
const ASSETS = [
  'GC=F',      // Gold
  'EURUSD=X',  // EUR/USD
  'GBPUSD=X',  // GBP/USD
  'JPY=X',     // USD/JPY
  'AUDUSD=X',  // AUD/USD
  'CAD=X',     // USD/CAD
  'CHF=X',     // USD/CHF
  'NZDUSD=X'   // NZD/USD
];

// 3. REGISTER NODE ROUTES
app.use('/api/trades', tradeRoutes);
app.use('/api/candles', candleRoutes);
app.use('/api/news', newsRoutes);

// 🛑 CRITICAL RE-ROUTE: 
// Instead of a direct proxy, we now go through the controller to fetch 3,000+ candles!
app.post('/api/analyze', analysisController.analyzePattern);

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
  if (timeframe === '15m') return timestamp - (timestamp % 900);
  if (timeframe === '1h') return timestamp - (timestamp % 3600);
  if (timeframe === '4h') return timestamp - (timestamp % 14400); 
  return timestamp;
};

const handleNewTick = async (data, symbol) => {
  try {
    const weekendFlag = isMarketClosed(data.time);
    const targetTimeframes = ['15m', '1h', '4h'];

    const updatePromises = targetTimeframes.map(async (tf) => {
      const bucketTime = getBucketTime(data.time, tf);
      const query = { symbol: symbol, time: bucketTime, timeframe: tf };
      const update = {
        $max: { high: data.high },
        $min: { low: data.low },
        $set: { close: data.close, isWeekend: weekendFlag },
        $setOnInsert: { symbol: symbol, open: data.open, time: bucketTime, timeframe: tf }
      };
      await Candle.findOneAndUpdate(query, update, { upsert: true, new: true });
    });

    await Promise.all(updatePromises);
    io.emit('price-update', { symbol, ...data });

  } catch (err) {
    console.error(`❌ DB SAVE FAILED for ${symbol}: `, err.message);
  }
};

// ==========================================
// 🔄 AUTO-CATCH-UP
// ==========================================
const syncHistoricalData = async () => {
    console.log("🔄 AUTO-CATCH-UP: Patching missing candles for ALL assets...");
    try {
        const startDate = new Date();
        startDate.setDate(startDate.getDate() - 7);
        const period1 = startDate.toISOString().split('T')[0];

        for (const symbol of ASSETS) {
            try {
                const result = await yahooFinance.chart(symbol, { period1, interval: '1h' });
                const rawData = result?.quotes || [];

                if (rawData.length === 0) continue;

                for (const tf of ['15m', '1h', '4h']) {
                    const bulkOps = rawData.map(candle => {
                        if (!candle.date || !candle.close) return null;
                        const timeInSeconds = Math.floor(new Date(candle.date).getTime() / 1000);
                        const bucketTime = getBucketTime(timeInSeconds, tf);

                        return {
                            updateOne: {
                                filter: { symbol: symbol, time: bucketTime, timeframe: tf },
                                update: {
                                    $max: { high: candle.high },
                                    $min: { low: candle.low },
                                    $set: { close: candle.close, isWeekend: isMarketClosed(timeInSeconds) },
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
// 🛡️ LIVE PRICE FEED
// ==========================================
const startLivePriceFeed = () => {
    console.log(`🔌 Starting Live Yahoo Finance Feed for 8 Assets...`);
    
    setInterval(async () => {
        const startDate = new Date();
        startDate.setDate(startDate.getDate() - 1);
        const period1 = startDate.toISOString().split('T')[0];

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

                    await handleNewTick(candlePayload, symbol);
                }
            } catch (error) {
                console.error(`⚠️ Yahoo Feed slight delay for ${symbol}...`);
            }
        });

        await Promise.allSettled(fetchPromises);

    }, 30000); 
};

const initializeDataEngines = async () => {
    await syncHistoricalData(); 
    startLivePriceFeed();       
    fetchLiveNews();            
};

initializeDataEngines();

cron.schedule('0 */4 * * *', () => syncHistoricalData());
cron.schedule('0 */6 * * *', () => fetchLiveNews());     

if (process.env.NODE_ENV === 'production' || process.env.RENDER_EXTERNAL_URL) {
  const RENDER_URL = process.env.RENDER_EXTERNAL_URL || 'https://aura-trade.onrender.com';
  setInterval(() => {
    axios.get(`${RENDER_URL}/healthcheck`).catch(() => {});
  }, 300000); 
}

server.listen(PORT, '0.0.0.0', () => {
  console.log(`🚀 Node.js Server running on port ${PORT}`);
  console.log(`🔌 Socket.io initialized`);
  console.log(`🧠 AI Matrix midwife active via AnalysisController`);
  
  // 🤖 NEW: Wake up the Telegram Bot Background Worker
  startTelegramBot(); 
});

module.exports = { app, server, io };