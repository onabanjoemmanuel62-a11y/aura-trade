// src/services/api.js
const API_URL = "https://aura-trade.onrender.com/"; // Ensure this matches your backend port

export const getAnalysis = async (timeframe = '1h') => {
    try {
        const response = await fetch(`${API_URL}/analyze/pattern`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ timeframe, currency: 'USD' })
        });
        return await response.json();
    } catch (error) {
        console.error("❌ Failed to fetch analysis:", error);
        return null;
    }
};