// Drag-to-spin frame-swap viewer for turntable ("frames" renderer) manifests.
// Preloads every frame first, then swaps images on horizontal drag with
// correct modulo wrap-around, flick inertia, and autospin-until-touched.
const FramesViewer = (() => {
  function create(host, frameUrls, onLoadProgress) {
    const n = frameUrls.length;
    const imgs = new Array(n);
    const wrap = document.createElement("div");
    wrap.style.cssText = "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden;";
    const imgEl = document.createElement("img");
    imgEl.style.cssText = "max-width:100%;max-height:100%;user-select:none;-webkit-user-drag:none;touch-action:none;";
    imgEl.draggable = false;
    wrap.appendChild(imgEl);
    host.appendChild(wrap);

    let loaded = 0;
    let cancelled = false;
    const loadPromises = frameUrls.map((url, i) => new Promise((resolve) => {
      const im = new Image();
      im.onload = () => { loaded++; onLoadProgress && onLoadProgress(loaded / n); resolve(); };
      im.onerror = () => { loaded++; onLoadProgress && onLoadProgress(loaded / n); resolve(); };
      im.src = url;
      imgs[i] = im;
    }));

    let index = 0;
    let dragging = false;
    let lastX = 0;
    let velocity = 0; // frames per ms, smoothed
    let lastMoveTime = 0;
    let autospin = null;
    const PIXELS_PER_FRAME = 8;

    function wrapIndex(i) {
      return ((i % n) + n) % n;
    }

    function render() {
      const im = imgs[wrapIndex(index)];
      if (im && im.complete && im.naturalWidth) imgEl.src = im.src;
    }

    function onDown(x) {
      dragging = true;
      lastX = x;
      lastMoveTime = performance.now();
      velocity = 0;
      stopAutospin();
    }
    function onMove(x) {
      if (!dragging) return;
      const now = performance.now();
      const dx = x - lastX;
      const framesDelta = -dx / PIXELS_PER_FRAME;
      if (Math.abs(framesDelta) >= 1) {
        index = wrapIndex(Math.round(index + framesDelta));
        render();
        lastX = x;
        const dt = Math.max(1, now - lastMoveTime);
        velocity = framesDelta / dt;
        lastMoveTime = now;
      }
    }
    function onUp() {
      if (!dragging) return;
      dragging = false;
      if (Math.abs(velocity) > 0.005) {
        inertia();
      }
    }

    function inertia() {
      let v = velocity;
      function step() {
        if (Math.abs(v) < 0.002) return;
        index = wrapIndex(Math.round(index + v * 16));
        render();
        v *= 0.92;
        requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    }

    function startAutospin() {
      stopAutospin();
      let last = performance.now();
      function step(t) {
        if (dragging) { autospin = null; return; }
        const dt = t - last;
        if (dt > 60) {
          index = wrapIndex(index + 1);
          render();
          last = t;
        }
        autospin = requestAnimationFrame(step);
      }
      autospin = requestAnimationFrame(step);
    }
    function stopAutospin() {
      if (autospin) cancelAnimationFrame(autospin);
      autospin = null;
    }

    wrap.addEventListener("mousedown", (e) => { e.preventDefault(); onDown(e.clientX); });
    window.addEventListener("mousemove", (e) => onMove(e.clientX));
    window.addEventListener("mouseup", onUp);
    wrap.addEventListener("touchstart", (e) => onDown(e.touches[0].clientX), { passive: true });
    wrap.addEventListener("touchmove", (e) => { onMove(e.touches[0].clientX); }, { passive: true });
    wrap.addEventListener("touchend", onUp);

    Promise.all(loadPromises).then(() => {
      if (cancelled) return;
      render();
      startAutospin();
    });

    return {
      destroy() {
        cancelled = true;
        stopAutospin();
        window.removeEventListener("mousemove", (e) => onMove(e.clientX));
        window.removeEventListener("mouseup", onUp);
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      },
    };
  }

  return { create };
})();
