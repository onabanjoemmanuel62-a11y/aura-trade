Your current `README.md` is still the default Create React App boilerplate. Here is the complete, production-ready `README.md` written from scratch for **AuraTrade**—covering the system architecture, the end-to-end data workflow, the Smart Money Concepts (SMC) engine rules, and local setup instructions so you can copy and paste it straight into your repository:

```markdown
# AuraTrade — Institutional SMC Forex & Gold Trading Platform

AuraTrade is a full-stack Smart Money Concepts (SMC) market analysis and automated paper-trading platform built for **Gold (`XAU/USD`)** and **7 Major Forex Pairs**. 

Instead of cluttering charts with lagging retail indicators, AuraTrade runs a custom Python quantitative engine (`AuraBrain v10.8`) that maps institutional market structure in real time—identifying Peak Formations, Change of Character (`CHoCH`), Break of Structure (`BOS`), unmitigated Order Blocks (`POI`), historical Inducement (`IDM`), and structural liquidity targets, all rendered on a clean, custom-drawn TradingView Lightweight Charts canvas.

---

##Supported Instruments

AuraTrade continuously tracks and analyzes 8 core pairs across `15M`, `1H`, and `4H` timeframes:

| Asset Class | Instrument | Yahoo Finance Ticker | Precision | Pip Size |
| :--- | :--- | :--- | :--- | :--- |
| **Commodities** | **XAU/USD** (Gold) | `GC=F` | 2 Decimals | `0.10` |
| **Forex Majors** | **EUR/USD** | `EURUSD=X` | 5 Decimals | `0.0001` |
| **Forex Majors** | **GBP/USD** | `GBPUSD=X` | 5 Decimals | `0.0001` |
| **Forex Majors** | **USD/JPY** | `JPY=X` | 3 Decimals | `0.01` |
| **Forex Majors** | **USD/CHF** | `CHF=X` | 5 Decimals | `0.0001` |
| **Forex Majors** | **AUD/USD** | `AUDUSD=X` | 5 Decimals | `0.0001` |
| **Forex Majors** | **USD/CAD** | `CAD=X` | 5 Decimals | `0.0001` |
| **Forex Majors** | **NZD/USD** | `NZDUSD=X` | 5 Decimals | `0.0001` |

---

## System Architecture & End-to-End Workflow

AuraTrade operates as a three-tier distributed system where data collection, quantitative analysis, and canvas rendering are cleanly separated:

```text
+-------------------------------------------------------------------------+
|                        1. NODE.JS BACKEND (Port 10000)                  |
|  - Polls Yahoo Finance every 30s for all 8 instruments                  |
|  - Upserts 15m, 1h, and 4h OHLCV candles into MongoDB                   |
|  - Evaluates open paper trades (TP1, SL, and 1R Break-Even)             |
|  - Broadcasts live ticks to the React frontend via Socket.io            |
+------------------------------------+------------------------------------+
                                     |
                     POST /api/analyze (3,000x 1H + 1,000x 4H candles)
                                     v
+-------------------------------------------------------------------------+
|                    2. PYTHON FASTAPI ENGINE (Port 8000)                 |
|  - Computes Wilder's ATR(14), RSI(14), and 50/200 EMAs (backend filter) |
|  - Maps Peak Formation High/Low anchors, CHoCH, and BOS sequences       |
|  - Locks unmitigated Pre-Breakout Order Blocks (ob_idx = break_idx - 1) |
|  - Detects 5-bar fractal Inducement (IDM) & structural liquidity targets|
|  - Evaluates closed 1H candle confirmations & locks active trades       |
+------------------------------------+------------------------------------+
                                     |
                      JSON Payload (Signals, Checklist, Visuals)
                                     v
+-------------------------------------------------------------------------+
|                         3. REACT FRONTEND (Port 3000)                   |
|  - ChartComponent.js: Renders custom HTML5 Canvas Primitives (Lines,    |
|    Order Block boxes, and dashed Trade Projections) on Lightweight v5   |
|  - SignalCard.js: Displays the 4-Step A+ Confluence Checklist, MTF      |
|    alignment, and integrated Lot Size & Risk Calculator                 |
|  - HistoryTable.js: Tracks verified paper-trade performance & win rate  |
+-------------------------------------------------------------------------+

```

### Step-by-Step Data & Execution Workflow

1. **Live Market Ingestion & Auto-Catch-Up (`server.js`):**
* On startup and every 4 hours via cron, the Node server backfills up to 7 days of historical `1H` candles across all 8 assets and aggregates them into `15m`, `1h`, and `4h` MongoDB collections (`models/Candle`).
* Every 30 seconds, the live price feed pulls `1m` quotes from `yahoo-finance2`, updates the current candle's High/Low/Close in MongoDB, evaluates open paper trades, and emits `price-update` over Socket.io.


2. **Multi-Timeframe Matrix Dispatch (`controllers/analysisController.js`):**
* When a user selects a pair on the dashboard, the Node backend queries the latest `3,000` 1H candles, `1,000` 4H candles, and high-impact economic news events from MongoDB, forwarding the payload to the Python microservice (`POST /api/analyze`).


3. **SMC Structural & Entry Evaluation (`brain.py`):**
* The Python microservice analyzes the `1H` structural cycle alongside `4H` EMA trend alignment.
* To prevent repainting on intra-hour price spikes, entry triggers are evaluated strictly on **closed 1H candles** (`DROP_FORMING_CANDLE = True`).


4. **Automated Paper Trade Lifecycle (`controllers/tradeController.js` & `HistoryTable.js`):**
* When all 4 confluences are met and a 1H confirmation candle closes inside the Order Block with at least a `1:1.5` Risk-to-Reward ratio, AuraTrade logs an `OPEN` paper trade in MongoDB (`models/Trade`) and sends an alert via the integrated Telegram Bot.
* Every 30 seconds, incoming ticks check open trades for **Break-Even (`1R` / `50% to TP1`)**, **Take Profit (`TP1`)**, or **Stop Loss (`SL`)**, automatically updating the verified performance ledger in `HistoryTable.js`.



---

## Core Smart Money Concepts (SMC) Engine Rules (`v10.8`)

AuraTrade's analytical engine (`brain.py`) enforces strict institutional trading rules to eliminate common retail algorithmic flaws like repainting levels or chasing live price:

### 1. Peak Formation & Causal Structure (`CHoCH` / `BOS`)

* **Causal Swing Detection:** Swings are detected using an adaptive volatility window (`adaptive_swing_order`) scaled by Wilder's `ATR(14)` prominence (`0.80 * ATR`).
* **Peak Formation Anchors:** Every structural cycle originates from a confirmed **Peak Formation Low (`W`)** in bullish trends or **Peak Formation High (`M`)** in bearish trends.
* **Change of Character (`CHoCH`) & Break of Structure (`BOS`):** The engine identifies the initial structural reversal (`CHoCH`) and tracks subsequent continuation breaks (`BOS`) only after a valid multi-bar pullback of at least `0.40 * ATR`.

### 2. Exact Pre-Breakout Candle Order Block (`ob_idx = break_idx - 1`)

* Rather than drawing arbitrary zones or walking back to already-mitigated swing extremes, AuraTrade defines the **Point of Interest (`POI` / Order Block)** strictly as the **candle immediately preceding the `BOS` or `CHoCH` breakout candle (`ob_idx = break_idx - 1`)** across its full High-to-Low range (capping only extreme anomaly spikes `> 1.15 * ATR`).
* **Fair Value Gap (`FVG`) Confluence:** The engine scans the 3-candle breakout cluster for an imbalance (`>= 0.05 * ATR`). While an `FVG` boosts setup confluence in the 4-step checklist, a direct breakout without an FVG (`Direct Break (No FVG)`) never blocks a valid `break_idx - 1` Order Block from being drawn.

### 3. Strict Mitigation Lifecycle vs. Active Trade Persistence

* **Invalidated / Blown-Through Zones:** If any candle after the breakout wicks past the back of the Order Block (`> 0.08 * ATR`) or closes beyond it—or if price already tapped the zone and completed its target move—the POI is marked `INVALIDATED` and removed from the chart to keep the canvas clean.
* **Untouched vs. Tapped Active Zones:**
* `UNTOUCHED`: Price has departed the Order Block by at least `0.35 * ATR` and has not yet pulled back. The chart renders the POI box and a dashed roadmap projection (`Now -> POI Entry Zone -> Target 2`).
* `TAPPED_ACTIVE`: Price pulls back into the POI and respects its outer boundary. The box upgrades to `ACTIVE BUY/SELL POI` and remains locked on the chart throughout confirmation and for the entire duration of the resulting trade.



### 4. Non-Repainting Historical Inducement (`$$$ Inducement (IDM)`)

* Internal pullback liquidity (`IDM` or `Range Liquidity EQL/EQH`) is scanned strictly across confirmed historical 5-bar fractals (`j <= total - 5`) that produced a real reaction (`>= 0.45 * ATR`).
* Once price sweeps the inducement level on its way to the Order Block, the label updates to `$$$ Inducement (IDM) SWEPT ✅` and never shifts or chases live candles.

### 5. Zero-Repainting Execution & Adaptive Break-Even (`SL -> BE 🛡️`)

* **Clean `WAIT` Mode:** While waiting for price to reach a POI, no cluttered `ENTRY / SL / TP` horizontal price lines are drawn on the chart—only the structural SMC roadmap.
* **Locked 1H Confirmation:** Once price taps the POI and closes a 1H rejection candle (bullish body/wick rejection for `BUY`, bearish body/wick rejection for `SELL`), `Entry`, `SL`, `TP1`, and `TP2` are locked in memory and MongoDB. They **never** shift with live ticks.
* **Automatic Break-Even Protection (`1R` / `50% TP1`):** As soon as an active trade moves **1R in profit** (equal to the initial Stop Loss distance) or reaches **50% of the distance to `TP1**`, the Stop Loss automatically moves to `Entry ± 1 pip buffer` (`SL (BE) 🛡️` in gold on the chart) so a sudden market reversal cannot turn a winning move into a loss.

---

## Project Directory Structure

```text
auratrade/
├── brain.py                        # Python FastAPI SMC Engine (Port 8000)
├── server.js                       # Node.js + Express + Socket.io Monolith (Port 10000)
├── config/
│   └── db.js                       # MongoDB connection setup
├── controllers/
│   ├── analysisController.js       # MTF candle aggregation & Python engine bridge
│   └── tradeController.js          # Paper-trade lifecycle, Break-Even & SL/TP evaluator
├── models/
│   ├── Candle.js                   # 15m, 1h, and 4h OHLCV schema
│   ├── Trade.js                    # Paper trade & performance history schema
│   └── NewsEvent.js                # High-impact economic calendar schema
├── routes/
│   ├── tradeRoutes.js              # GET /api/trades endpoints
│   ├── candleRoutes.js             # GET /api/candles/:timeframe endpoints
│   └── newsRoutes.js               # GET /api/news endpoints
├── scripts/
│   ├── fetchLiveNews.js            # Economic calendar scraper/fetcher
│   └── telegramBot.js              # Telegram Sniper Bot alert worker
└── src/                            # React Frontend
    ├── App.js                      # Main dashboard layout & pair selector
    ├── ChartComponent.js           # Lightweight Charts v5 + Custom Canvas Primitives
    ├── SignalCard.js               # AI Brain panel (Signal, 4-Step Checklist, Risk Calc, MTF)
    └── HistoryTable.js             # Verified AI Performance table & filterable trade log

```

---

## Getting Started (Local Development)

### Prerequisites

* **Node.js** `v18+` and `npm`
* **Python** `3.10+` and `pip`
* **MongoDB** (Local instance or MongoDB Atlas URI)

### 1. Clone & Install Dependencies

```bash
# Install Node.js backend & React frontend dependencies
npm install

# Install Python SMC Engine dependencies
pip install fastapi uvicorn pandas numpy httpx ta pydantic

```

### 2. Environment Variables (`.env`)

Create a `.env` file in the root directory for the Node.js server:

```env
PORT=10000
MONGO_URI=your_mongodb_connection_string
PYTHON_API_URL=[http://127.0.0.1:8000/api/analyze](http://127.0.0.1:8000/api/analyze)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_optional
TELEGRAM_CHAT_ID=your_telegram_chat_id_optional

```

### 3. Run All Three Services

Open three terminal tabs to run the full stack locally:

**Terminal 1 — Start the Python SMC Brain (`Port 8000`):**

```bash
python brain.py

```

**Terminal 2 — Start the Node.js & Socket.io Backend (`Port 10000`):**

```bash
node server.js

```

**Terminal 3 — Start the React Frontend (`Port 3000`):**

```bash
npm start

```

Open [http://localhost:3000](http://localhost:3000) in your browser to view the live AuraTrade terminal.

---

## Key API Endpoints

| Method | Endpoint | Service | Description |
| --- | --- | --- | --- |
| `POST` | `/api/analyze` | Node -> Python | Fetches `1H` & `4H` candles from MongoDB, runs `brain.py` SMC analysis, and syncs active paper trades. |
| `POST` | `/api/debug` | Python (`8000`) | Returns raw internal structural diagnostics (`anchor`, `lines`, `poi_info`, `liq_data`, `ACTIVE_TRADES`). |
| `GET` | `/api/candles/:tf` | Node (`10000`) | Returns historical OHLCV candles (`15m`, `1h`, `4h`) with pagination support (`limit`, `before`). |
| `GET` | `/api/trades` | Node (`10000`) | Returns all logged paper trades (`OPEN`, `WON`, `LOST`) for the Verified AI Performance table. |
| `GET` | `/api/news` | Node (`10000`) | Returns upcoming and historical high-impact economic calendar events. |
| `GET` | `/healthcheck` | Node (`10000`) | Service health verification endpoint for Render uptime monitoring. |

---

## Production Deployment

* **Frontend:** Deployed on **Vercel** (`npm run build`).
* **Node.js & Python Backend:** Hosted on **Render** (`https://aura-trade-v1.onrender.com`) with automated 5-minute keep-alive health checks and 4-hour historical candle catch-up jobs.

```

```