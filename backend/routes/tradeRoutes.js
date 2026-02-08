const express = require('express');
const router = express.Router();
const { getTrades } = require('../controllers/tradeController');

// Map the root '/' of this route to the getTrades function
router.get('/', getTrades);

module.exports = router;