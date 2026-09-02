// Orbit web client config.
//
// The API base is derived from where this page was loaded from:
//   * on port 5173 (the standalone dev server) the API is a sibling on :8080
//   * anywhere else — including when the Go API serves this page itself, and
//     when it is reached through an https tunnel — the API is the SAME origin.
// That second case is what makes phone access work through a single URL.
// Override by setting window.ORBIT_API_BASE before this script loads.
(function () {
  const loc = window.location;
  const host = loc.hostname || "localhost";
  const isDevServer = loc.port === "5173";

  const httpBase = isDevServer
    ? `${loc.protocol}//${host}:8080`
    : loc.origin;

  const wsProto = loc.protocol === "https:" ? "wss:" : "ws:";
  const wsBase = isDevServer
    ? `${wsProto}//${host}:8080`
    : `${wsProto}//${loc.host}`;

  window.ORBIT_API_BASE = window.ORBIT_API_BASE || httpBase;
  window.ORBIT_WS_BASE = window.ORBIT_WS_BASE || wsBase;
})();
