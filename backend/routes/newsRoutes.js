const express = require('express');
const router = express.Router();
const NewsEvent = require('../models/NewsEvent');

// @desc    Get News Events (Filtered by Date Range OR Latest 100)
// @route   GET /api/news?start=...&end=... OR GET /api/news
router.get('/', async (req, res) => {
  try {
    const { start, end } = req.query;
    let events = [];

    // CASE 1: Date Range Provided (Zooming/Scrolling)
    if (start && end) {
      const query = {
        time: { 
          $gte: parseInt(start), 
          $lte: parseInt(end) 
        }
      };

      // Fetch specific range, Oldest -> Newest
      events = await NewsEvent.find(query)
        .select('time originalId currency event impact actual forecast -_id')
        .sort({ time: 1 });
    } 
    
    // CASE 2: No Dates Provided (Initial Chart Load)
    else {
      // Fetch the LAST 100 events (Newest First)
      const recentEvents = await NewsEvent.find()
        .sort({ time: -1 }) // Get 2025/2026 data
        .limit(100)
        .select('time originalId currency event impact actual forecast -_id');

      // Reverse to Chronological (Oldest -> Newest) for the chart
      events = recentEvents.reverse();
    }

    res.json(events);

  } catch (error) {
    console.error("News Fetch Error:", error);
    res.status(500).json({ message: "Failed to fetch news" });
  }
});

module.exports = router;