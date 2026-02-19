PROJECT: AuraTrade AI (XAUUSD SMC System)
STATUS: [Phase 4: Institutional AI & UI Synchronization - LIVE]
1. TECH STACK (Confirmed)
Frontend: React (Vercel) - Running Lightweight Charts v5 with Custom SMC BoxRenderer.

Backend Manager: Node.js/Express (Render) - Auto-Patching Yahoo Finance GC=F Live Feed + News Fetcher.

Logic Engine: Python (aura_brain/brain.py) - Pure SMC + Fundamental Logic Engine.

Database: MongoDB (Stores Continuous, Clean Candle Data & News)

2. COMPLETED TASKS (The Architecture & Data Pipeline)
[x] Audit Architecture: Discovered Hybrid Node.js/Python system.

[x] Solve "Split Brain": Python no longer reads static CSVs; it accepts live 3000-candle payloads.

[x] Fix "Data Pollution": Backfilled pure Gold Futures (GC=F) data.

[x] Fix "Whitespace Gap": Upgraded ChartComponent.js to strip weekend/closed-market hours for a continuous layout.

[x] Implement Auto-Catch-Up: Node.js seamlessly patches offline gaps on boot.

[x] UPGRADE: Pure SMC Math Engine: Replaced historical fractal guessing with strict Order Block proximity math.

[x] UPGRADE: Institutional Trend Lock: AI uses 50/200 EMA to lock trading direction (No buying in downtrends).

[x] UPGRADE: Liquidity Sweep Detector: AI scans for "Stop Hunts" (wicks piercing OBs) to boost confidence.

[x] UPGRADE: Fundamental News Injection: Node.js sends the latest High-Impact USD News (Actual vs Forecast) to Python to confirm or block technical setups.

3. CRITICAL SOLUTIONS
How does the AI know the trend? [SOLVED]

Solution: Python calculates the 50 EMA and 200 EMA on the fly. If the 50 crosses below the 200, the AI strictly ignores all Bullish Demand zones.

How does the AI factor in Forex Factory News? [SOLVED]

Solution: Node.js fetches the latest outcome (e.g., GDP Actual > Forecast) from MongoDB and packages it with the candles. Python reads the USD strength and applies it inversely to Gold (XAUUSD).

4. CURRENT TASK: DEPLOYMENT & VERIFICATION
[x] Deploy Python Engine: brain.py upgraded and live on Render.

[x] Deploy Node.js Controller: analysisController.js upgraded and live on Render.

[ ] Update React UI (Pending): Modify the SignalCard component to .map() and render the Python reasoning array so the user can read the AI's logic.

5. KNOWN RISKS / NEXT STEPS
Order Block Clutter: Now that Python sends all unmitigated zones, the chart might look busy. If needed, we can limit Python to only send the top 3 closest zones.

Strictness Limit: The AI will confidently stay at 0% until the price enters the "strike zone" (within 3x ATR of an Order Block). This requires patience from the trader.