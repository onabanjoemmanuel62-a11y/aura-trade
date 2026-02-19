PROJECT: AuraTrade AI (XAUUSD SMC System)
STATUS: [Phase 3: Real-Time Polish & Client Verification - LIVE]
1. TECH STACK (Confirmed)
Frontend: React (Vercel) - Running Lightweight Charts v5 with Custom SMC BoxRenderer.

Backend Manager: Node.js/Express (Render) - Auto-Patching Yahoo Finance GC=F Live Feed.

Logic Engine: Python (aura_brain/brain.py) - Stateless SMC Engine processing 3000 live candles.

Database: MongoDB (Stores Continuous, Clean Candle Data & News)

Communication: HTTP Bridge + WebSockets (Node.js -> Python -> React)

2. COMPLETED TASKS (The Architecture & Data Pipeline)
[x] Audit Architecture: Discovered Hybrid Node.js/Python system.

[x] Solve "Split Brain": Python no longer reads static CSVs; it accepts live data.

[x] Refactor Python Engine: brain.py calculates Order Blocks & Fractals on the fly.

[x] Refactor Node.js Bridge: analysisController.js fetches 3000 MongoDB candles and sends them to Python.

[x] Update Frontend UI: SignalCard.js displays Entry, SL, TP, and SMC logic.

[x] Fix "Data Pollution": Nuked old Crypto-Gold (PAXGUSDT) data and backfilled pure Gold Futures (GC=F) data.

[x] Fix "Whitespace Gap": Upgraded ChartComponent.js to strip weekend/closed-market hours for a continuous TradingView look.

[x] Implement Auto-Catch-Up: Node.js seamlessly patches offline gaps with Yahoo Finance historical data on boot before starting the 30-second live tick poller.

3. CRITICAL SOLUTIONS
How does Node.js talk to Python? [SOLVED]

Solution: Node.js uses axios to POST 3000 live candles to the Python /api/analyze endpoint.

Where is the logic? [SOLVED]

Solution: "Retail" logic (RSI/Patterns) in Node.js was replaced by "SMC" logic (Order Blocks) in Python.

Why was the chart blank / crashing? [SOLVED]

Solution: Lightweight Charts v5 deprecated addCandlestickSeries. Upgraded the custom SMC BoxRenderer to v5 syntax and handled setMarkers safety checks.

4. CURRENT TASK: DEPLOYMENT & VERIFICATION
[x] Environment Variables: PYTHON_API_URL set in Render/Node.js environment settings.

[x] Server Restart: Both Node.js and Python services restarted to load the Auto-Catch-Up code.

[x] "Wake Up" Test: Verified that the frontend moves from "Simulating..." to showing actual Order Block data on the chart.

5. KNOWN RISKS / NEXT STEPS
Latency: Monitor Python timeout length. The AI is now searching for complex Fractals across 3,000 candles (up from 300) every time a user requests an analysis.

Live Stream Resilience: Monitor the Yahoo Finance 30-second polling. If Yahoo rate-limits the server, the chart will temporarily pause until the next successful poll.