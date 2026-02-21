PROJECT: AuraTrade AI (XAUUSD SMC System)
STATUS: [Phase 7: Multi-Timeframe Matrix (4H/1H Alignment) - IN PROGRESS]
1. TECH STACK (Confirmed)
Frontend: React (Vercel) - Running Lightweight Charts v5 with Custom SMC BoxRenderer & dynamic BOSRenderer (CHoCH/BOS detection enabled).

Backend Manager: Node.js/Express (Render) - Auto-Patching Yahoo Finance GC=F Live Feed + Multi-News Fetcher.

Logic Engine: Python (aura_brain/brain.py) - Strict SMC Engine powered by a live scikit-learn Machine Learning model (aura_model.pkl).

Database: MongoDB (Stores Continuous, Clean Candle Data & News)

2. COMPLETED TASKS (Architecture, UI, and ML)
[x] Solve "Split Brain": Python accepts live 3000-candle payloads from Node.js.

[x] Fix "Whitespace Gap": Upgraded React chart to strip closed-market hours for a continuous layout.

[x] Institutional Trend Lock: AI uses 50/200 EMA to lock trading direction.

[x] Strict FVG & Mitigation Filter: brain.py requires FVG + ATR displacement. React visually truncates mitigated OBs into dashed footprints.

[x] Dynamic Structural Breaks (CHoCH vs BOS): Python mathematically analyzes swing peak sequences to label breaks as trend-shifting (CHoCH) or trend-continuing (BOS).

[x] Feature Engineering: Built Python extraction script to convert 20 years of SMC Gold data into machine-readable parameters.

[x] Backtest Labeling: Auto-graded historical OBs as Winners (1) or Losers (0) using an automated simulation loop.

[x] Model Training: Trained a Random Forest Classifier to replace the hardcoded confidence point system, achieving a profitable baseline edge.

[x] Live Inference: Successfully injected the aura_model.pkl Brain into the production backend for real-time win probability scoring.

3. CRITICAL SOLUTIONS
How do we accurately label CHoCH vs BOS? [SOLVED]

Solution: Python's logic engine looks at the sequence of structural breaks. If a bullish move breaks a lower high, it flags the frontend to render "CHoCH". If it breaks a higher high, it renders "BOS".

How do we transition from Rule-Based points to True AI? [SOLVED]

Solution: Built a local data harvester to map 20 years of Gold OHLC data against 17 years of Forex Factory news. Trained a .pkl Random Forest model on features like FVG Size, RSI, Momentum Ratio, and News Bias to output a literal mathematical win probability.

4. CURRENT TASK: MULTI-TIMEFRAME MATRIX (4H/1H)
[ ] Node.js Payload Upgrade: Update the backend manager to fetch and send both 1H and 4H candle arrays to Python simultaneously.

[ ] 4H Trend Detection: Upgrade brain.py to calculate the 4H macro trend (using 4H EMAs and 4H Swing Structure).

[ ] The Matrix Filter: Restrict the 1H ML execution so it completely ignores 1H Bullish Order Blocks if the 4H macro chart is in a structural downtrend.