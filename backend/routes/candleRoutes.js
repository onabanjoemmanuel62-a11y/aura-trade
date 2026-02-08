const express = require('express');
const router = express.Router();
const { getCandles } = require('../controllers/candleController');

router.get('/:timeframe', getCandles);

module.exports = router;