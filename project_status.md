# PROJECT: AuraTrade AI (XAUUSD SMC System)
# STATUS: [Phase 2: Core Integration - READY FOR DEPLOYMENT]

## 1. TECH STACK (Confirmed)
- Frontend: React (Vercel) - *Updated `SignalCard.js`*
- Backend Manager: Node.js/Express (Render) - *Updated `analysisController.js`*
- Logic Engine: Python (`aura_brain/brain.py`) - *Updated to Stateless SMC Engine*
- Database: MongoDB (Stores Candle Data & News)
- Communication: HTTP Bridge (Node.js sends JSON -> Python analyzes -> Returns JSON)

## 2. COMPLETED TASKS (The "Half-Made" Fixes)
- [x] **Audit Architecture:** Discovered Hybrid Node.js/Python system.
- [x] **Solve "Split Brain":** Python no longer reads static CSVs; it accepts live data.
- [x] **Refactor Python Engine:** `brain.py` now calculates Order Blocks & Fractals on the fly.
- [x] **Refactor Node.js Bridge:** `analysisController.js` now fetches MongoDB candles and sends them to Python.
- [x] **Update Frontend UI:** `SignalCard.js` now displays Entry, SL, TP, and SMC logic.

## 3. CRITICAL SOLUTIONS
- **How does Node.js talk to Python?** [SOLVED]
  - Solution: Node.js uses `axios` to POST live candle data to the Python `/api/analyze` endpoint.
- **Where is the logic?** [SOLVED]
  - Solution: "Retail" logic (RSI/Patterns) in Node.js was replaced by "SMC" logic (Order Blocks) in Python.

## 4. CURRENT TASK: DEPLOYMENT & VERIFICATION
- [ ] **Environment Variables:** Must set `PYTHON_API_URL` in Render/Node.js environment settings.
- [ ] **Server Restart:** Both Node.js and Python services need a restart to load new code.
- [ ] **"Wake Up" Test:** Verify that the frontend moves from "Simulating..." to showing actual Order Block data.

## 5. KNOWN RISKS / NEXT STEPS
- **Latency:** Watch for timeouts if Python takes too long to calculate Fractals (current limit: 300 candles).
- **Data Quality:** Ensure MongoDB has *fresh* data (is the `fetchLiveNews` or a candle scraper running?).