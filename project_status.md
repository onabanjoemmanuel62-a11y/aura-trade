# PROJECT: AuraTrade AI (XAUUSD SMC System)
# STATUS: [Phase 1: Code Audit - Hybrid Architecture Discovery]

## 1. TECH STACK
- Frontend: React (Vercel)
- Backend API: Node.js/Express (Render)
- Logic Engine: Python (`aura_brain/brain.py`) ? TBD
- Database: MongoDB (Implied by `models/` folder structure)

## 2. FILE STRUCTURE AUDIT
- `controllers/analysisController.js`: Likely the API endpoint that triggers analysis.
- `aura_brain/brain.py`: The likely home of the SMC Algorithm.
- `scripts/fetchLiveNews.js`: Handles the "News Alert" feature seen in UI.

## 3. CRITICAL QUESTION
- How does Node.js talk to Python? (Child Process? HTTP? Or is Python unused?)

## 4. NEXT STEPS
- Review code of `analysisController.js` vs `brain.py`.
- Determine which "Brain" to keep.