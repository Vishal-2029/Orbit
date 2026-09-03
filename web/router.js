// Minimal hash router.
// Routes:
//   #/                -> home
//   #/capture/:id      -> capture screen
//   #/processing/:id   -> processing screen
//   #/view/:slug       -> viewer (by slug, public share link)
//   #/view-id/:id      -> viewer (by capture id, used right after finishing)
const Router = (() => {
  const app = document.getElementById("app");
  let currentCleanup = null;

  function parse() {
    const hash = location.hash.replace(/^#\/?/, "");
    const parts = hash.split("/").filter(Boolean);
    return parts;
  }

  async function render() {
    if (currentCleanup) {
      try { currentCleanup(); } catch (e) { console.error(e); }
      currentCleanup = null;
    }
    app.innerHTML = "";
    const parts = parse();
    try {
      if (parts.length === 0) {
        currentCleanup = await ScreenHome.mount(app);
      } else if (parts[0] === "capture" && parts[1]) {
        currentCleanup = await ScreenCapture.mount(app, parts[1]);
      } else if (parts[0] === "processing" && parts[1]) {
        currentCleanup = await ScreenProcessing.mount(app, parts[1]);
      } else if (parts[0] === "view" && parts[1]) {
        currentCleanup = await ScreenViewer.mount(app, { slug: parts[1] });
      } else if (parts[0] === "view-id" && parts[1]) {
        currentCleanup = await ScreenViewer.mount(app, { id: parts[1] });
      } else {
        location.hash = "#/";
      }
    } catch (err) {
      console.error(err);
      app.innerHTML = `<div class="container"><div class="card"><h2>Something went wrong</h2><p class="muted">${escapeHtml(err.message || String(err))}</p><button class="primary" onclick="location.hash='#/'">Back home</button></div></div>`;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  window.escapeHtml = escapeHtml;

  window.addEventListener("hashchange", render);
  window.addEventListener("DOMContentLoaded", render);
  if (document.readyState !== "loading") render();

  // Re-mounting the route you are already on cannot go through navigate():
  // assigning the hash its current value fires no hashchange, so nothing
  // re-renders. Reprocessing a capture needs exactly that.
  return { navigate: (h) => (location.hash = h), reload: render };
})();
