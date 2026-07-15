const express = require('express');
const router = express.Router();
const { getTrades, getOpenTrades } = require('../controllers/tradeController');

// @route   GET /api/trades/open
// IMPORTANT: this specific route must be registered BEFORE the generic '/' route
// below, otherwise Express would need param-based disambiguation. Since both are
// plain paths here (no conflict), order doesn't strictly matter, but keeping the
// more specific route first is good practice.
router.get('/open', getOpenTrades);

// @route   GET /api/trades?status=OPEN|CLOSED
router.get('/', getTrades);

module.exports = router;