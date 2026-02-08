/**
 * 🧠 News Logic Helper
 * Determines trade bias based on Economic Data deviation.
 */

const calculateNewsBias = (event, currencyPair = 'XAUUSD') => {
  // 1. Safety Check: Ensure we have data to compare
  if (!event || event.actual === null || event.forecast === null) {
    return 'NEUTRAL';
  }

  // 2. Impact Check: We only trade 'High' impact news
  // CSV Format: "High Impact Expected"
  const isHighImpact = event.impact && event.impact.toLowerCase().includes('high');
  
  if (!isHighImpact) {
    return 'NEUTRAL'; // Ignore low/medium impact events
  }

  // 3. Calculate Deviation (The Surprise Factor)
  const deviation = event.actual - event.forecast;

  // 4. Determine Currency Sentiment (e.g., Is USD Strong or Weak?)
  // Logic: Positive Deviation (> 0) = Bullish Currency
  //        Negative Deviation (< 0) = Bearish Currency
  let currencySentiment = 'NEUTRAL';

  if (deviation > 0) {
    currencySentiment = 'BULLISH';
  } else if (deviation < 0) {
    currencySentiment = 'BEARISH';
  }

  // 5. Map to Pair Strategy (The Inversion)
  // Context: We are trading Gold (XAUUSD or PAXGUSD).
  // Gold is priced in USD. Therefore:
  // Strong USD (Bullish) -> Gold goes DOWN -> SELL
  // Weak USD (Bearish)   -> Gold goes UP   -> BUY

  if (event.currency === 'USD') {
    if (currencySentiment === 'BULLISH') return 'SELL';
    if (currencySentiment === 'BEARISH') return 'BUY';
  }

  // Future expansion: If trading EURUSD and event is EUR...
  // if (event.currency === 'EUR' && currencyPair === 'EURUSD') {
  //    if (currencySentiment === 'BULLISH') return 'BUY';
  //    if (currencySentiment === 'BEARISH') return 'SELL';
  // }

  return 'NEUTRAL';
};

module.exports = { calculateNewsBias };