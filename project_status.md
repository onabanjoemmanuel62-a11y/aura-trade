PROJECT: AuraTrade AI (XAUUSD SMC System)
STATUS: [Phase 6: Machine Learning Evolution & Training - IN PROGRESS]
1. TECH STACK (Confirmed)
Frontend: React (Vercel) - Running Lightweight Charts v5 with Custom SMC BoxRenderer & BOSRenderer (High-Visibility enabled).

Backend Manager: Node.js/Express (Render) - Auto-Patching Yahoo Finance GC=F Live Feed + Multi-News Fetcher.

Logic Engine: Python (aura_brain/brain.py) - Strict SMC (FVG/BSL/SSL/BOS) Engine.

Database: MongoDB (Stores Continuous, Clean Candle Data & News)

2. COMPLETED TASKS (The Architecture & Data Pipeline)
[x] Solve "Split Brain": Python no longer reads static CSVs; it accepts live 3000-candle payloads.

[x] Fix "Whitespace Gap": Upgraded ChartComponent.js to strip weekend/closed-market hours for a continuous layout.

[x] UPGRADE: Institutional Trend Lock: AI uses 50/200 EMA to lock trading direction.

[x] UPGRADE: Liquidity Sweep Detector: AI scans for "Stop Hunts" (wicks piercing OBs) to boost confidence.

[x] UPGRADE: Strict FVG Filter: brain.py requires a Fair Value Gap and massive ATR displacement to validate an Order Block.

[x] UPGRADE: Mitigation Footprints: React and Python truncate mitigated OBs, fading them into historical footprints.

[x] UPGRADE: Static Liquidity Targets (BSL/SSL): AI uses scipy wave detection to find confirmed historical swing highs/lows for exact Take Profit targeting.

[x] UPGRADE: True Structural BOS: Python scans for previous structural swing highs/lows and React draws high-visibility dashed lines specifically where the break occurs.

3. CRITICAL SOLUTIONS
How do we identify a True BOS instead of a Micro-Break? [SOLVED]

Solution: Python's detect_smc_structures looks backward to find the argrelextrema peak that formed before the Order Block, then iterates forward to find the exact timestamp that peak was broken, sending the (X,Y) coordinates to React.

4. CURRENT TASK: MACHINE LEARNING TRAINING
[ ] Feature Engineering: Build an extraction script to convert SMC data into machine-readable parameters.

[ ] Backtest Labeling: Create a script to auto-grade historical OBs as Winners (1) or Losers (0).

[ ] Model Training: Train an XGBoost/Random Forest Classifier to replace the hardcoded confidence point system.

[ ] Live Inference: Connect the trained .pkl model into brain.py for live probability scoring.