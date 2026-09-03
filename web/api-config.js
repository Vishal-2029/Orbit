// Points the web client at a separately hosted API (Vercel frontend + Render API).
//
// config.js derives the API from the page origin, which is correct when the Go
// API serves this page itself. When the frontend is hosted elsewhere, that
// guess is wrong, so set the two bases here first — config.js keeps whatever is
// already set. Leave the file as-is for same-origin deployments.
//
// window.ORBIT_API_BASE = "https://your-api.onrender.com";
// window.ORBIT_WS_BASE  = "wss://your-api.onrender.com";

window.ORBIT_API_BASE = "https://orbit-6ux0.onrender.com";
window.ORBIT_WS_BASE  = "wss://orbit-6ux0.onrender.com";