// Drag-to-spin frame-swap viewer for turntable ("frames" renderer) manifests.
// Preloads every frame first, then swaps images on drag with correct
// modulo wrap-around, flick inertia, and autospin-until-touched.
//
// Spin captures are shot from three heights, so the frames form a grid rather
// than a single ring: dragging sideways turns the object, dragging up and down
// changes the height you are looking from. With one row it behaves exactly as
// the old single-axis viewer did.
const FramesViewer = (() => {
  // Group frames into rows of equal height using the angles the manifest
  // carries. Falls back to one row when there are no angles, which is what the
  // stitch-failure fallback produces.
  function buildGrid(n, yaws, pitches) {
    if (!pitches || pitches.length !== n || !yaws || yaws.length !== n) {
      return [Array.from({ length: n }, (_, i) => i)];
    }
    const byPitch = new Map();
    for (let i = 0; i < n; i++) {
      // Round so tiny sensor differences do not split one row in two.
      const key = Math.round(pitches[i] / 15) * 15;
      if (!byPitch.has(key)) byPitch.set(key, []);
      byPitch.get(key).push(i);
    }
    // Highest row first, so dragging up moves toward the top of the object.
    const rows = [...byPitch.entries()].sort((a, b) => b[0] - a[0]).map(([, idx]) =>
      idx.sort((a, b) => yaws[a] - yaws[b]));
    // A row with a single frame is noise, not a viewing height - fold it back
    // in rather than letting a drag land on a row that cannot turn.
    if (rows.length > 1 && rows.every((r) => r.length > 1)) return rows;
    // Collapsing has to re-sort by angle, not just concatenate the rows. A
    // capture that fell back to this viewer usually has a clean ring plus a
    // ceiling and floor shot; joining the groups end to end put the ceiling
    // first and the floor last, so dragging jumped backwards mid-turn instead
    // of going round once.
    return [Array.from({ length: n }, (_, i) => i).sort((a, b) => yaws[a] - yaws[b])];
  }

  function create(host, frameUrls, onLoadProgress, angles) {
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

    const grid = buildGrid(n, angles && angles.yaws, angles && angles.pitches);
    let row = Math.floor(grid.length / 2);   // start level with the object
    let index = 0;
    let dragging = false;
    let lastX = 0;
    let lastY = 0;
    // How far you drag vertically to change viewing height. Deliberately larger
    // than the horizontal step: rows are few, and an accidental vertical wobble
    // during a sideways spin should not jump the camera up and down.
    const PIXELS_PER_ROW = 70;
    let velocity = 0; // frames per ms, smoothed
    let lastMoveTime = 0;
    let autospin = null;
    // How far you drag to advance one frame. Larger = slower, finer control.
    // At 8px a small flick tore through the whole spin in an instant.
    const PIXELS_PER_FRAME = 26;
    // Milliseconds per frame when spinning on its own. 60ms was a blur; this is
    // a slow, readable turn - a full circle takes about n * 0.18 seconds.
    const AUTOSPIN_MS_PER_FRAME = 180;
    // How quickly a flick runs out of momentum. Lower = stops sooner.
    const INERTIA_DECAY = 0.90;
    const MAX_FLICK_SPEED = 0.012;   // frames per ms

    function wrapIndex(i) {
      const len = grid[row].length;
      return ((i % len) + len) % len;
    }

    function render() {
      const im = imgs[grid[row][wrapIndex(index)]];
      if (im && im.complete && im.naturalWidth) imgEl.src = im.src;
    }

    // Rows do not wrap: the top of an object is the top. Clamping stops a drag
    // from looping from above straight back to below.
    function setRow(next) {
      const clamped = Math.max(0, Math.min(grid.length - 1, next));
      if (clamped === row) return;
      // Keep pointing at the same side of the object when changing height.
      const frac = index / grid[row].length;
      row = clamped;
      index = wrapIndex(Math.round(frac * grid[row].length));
      render();
    }

    function onDown(x, y) {
      dragging = true;
      lastX = x;
      lastY = y;
      lastMoveTime = performance.now();
      velocity = 0;
      stopAutospin();
    }
    function onMove(x, y) {
      if (!dragging) return;
      const now = performance.now();

      if (grid.length > 1) {
        const dy = y - lastY;
        if (Math.abs(dy) >= PIXELS_PER_ROW) {
          // Drag up -> look from higher up, which is the row above.
          setRow(row + (dy < 0 ? -1 : 1));
          lastY = y;
          return;
        }
      }

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
      // Cap the launch speed: without this a fast swipe on a phone spun the
      // object so quickly the frames were unreadable.
      let v = Math.max(-MAX_FLICK_SPEED, Math.min(MAX_FLICK_SPEED, velocity));
      let carry = 0;
      function step() {
        if (Math.abs(v) < 0.0015) return;
        // Accumulate fractional frames so slow speeds still advance smoothly
        // instead of rounding to zero and stalling.
        carry += v * 16;
        const whole = Math.trunc(carry);
        if (whole !== 0) {
          carry -= whole;
          index = wrapIndex(index + whole);
          render();
        }
        v *= INERTIA_DECAY;
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
        if (dt > AUTOSPIN_MS_PER_FRAME) {
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

    // Named so destroy() can actually remove them; the old anonymous arrows
    // created fresh functions and removed nothing.
    const onMouseMove = (e) => onMove(e.clientX, e.clientY);
    wrap.addEventListener("mousedown", (e) => { e.preventDefault(); onDown(e.clientX, e.clientY); });
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onUp);
    wrap.addEventListener("touchstart", (e) => onDown(e.touches[0].clientX, e.touches[0].clientY), { passive: true });
    wrap.addEventListener("touchmove", (e) => { onMove(e.touches[0].clientX, e.touches[0].clientY); }, { passive: true });
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
        window.removeEventListener("mousemove", onMouseMove);
        window.removeEventListener("mouseup", onUp);
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      },
    };
  }

  return { create };
})();
