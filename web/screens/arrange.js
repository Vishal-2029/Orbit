// Place the photos by hand, and lock them there.
//
// A solver works from what photos have in common, and some photos have too
// little to go on: a window of repeating mullions matches one bay to the next
// and means it; a blank wall matches nothing at all. When the machine cannot
// tell, the person looking at the picture can.
//
// So this shows every photo WHOLE - no seam cut, no blend, nothing hidden - at
// the position it would actually be placed, on a flat map of the sphere. Drag
// one where it belongs, click it to lock it there, and build. A locked photo is
// an instruction: the build puts it exactly there and no solve may move it.
//
// The map is equirectangular: left to right is all the way round, top to bottom
// is straight up to straight down. A photo is drawn as a rectangle of its own
// angular size, which is honest near the horizon and increasingly generous
// towards the poles - the same distortion the finished 360 has, and the reason
// the ceiling shot looks so wide.
const ScreenArrange = (() => {
  // Assumed width of one photo, in degrees, when nothing better is known.
  // Only affects how big a photo is DRAWN here; the build measures the real
  // field of view from the photos themselves.
  const DEFAULT_HFOV = 63;

  const TAU = Math.PI * 2;

  function wrapYaw(y) {
    return ((y + Math.PI) % TAU + TAU) % TAU - Math.PI;
  }

  async function mount(app, captureId) {
    let capture, frames, manifest = null;
    try {
      const [cap, fr] = await Promise.all([
        OrbitAPI.getCapture(captureId),
        OrbitAPI.listFrames(captureId),
      ]);
      capture = cap.capture;
      frames = (fr.frames || []).filter((f) => !f.excluded);
      // Where the last build put each photo. Absent on a capture that has
      // never been built, and on one built before positions were recorded -
      // the sensor's own angles stand in, which is what the build would have
      // started from anyway.
      manifest = await OrbitAPI.getManifest(captureId).catch(() => null);
    } catch (e) {
      app.innerHTML = `<div class="container"><div class="card"><h2>Not found</h2>
        <p class="muted">${escapeHtml(e.message)}</p>
        <button class="primary" onclick="location.hash='#/'">Back home</button></div></div>`;
      return () => {};
    }

    const placed = {};
    ((manifest && manifest.photos) || []).forEach((p) => { placed[p.index] = p; });

    const num = PhotoNames.numbers(frames);
    // One entry per photo: where it sits now, whether it is pinned, and the
    // picture itself. Positions are radians in the viewer's frame throughout,
    // which is what the server stores and what the build reads back.
    const items = frames.map((f) => {
      const p = placed[f.index];
      const manual = f.manual_yaw != null && f.manual_pitch != null;
      const yaw = manual ? f.manual_yaw
        : p ? p.yaw : wrapYaw((Number(f.yaw) || 0) * Math.PI / 180);
      const pitch = manual ? f.manual_pitch
        : p ? p.pitch : -(Number(f.pitch) || 0) * Math.PI / 180;
      const roll = manual ? (f.manual_roll || 0) : (p ? (p.roll || 0) : 0);
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.src = OrbitAPI.imageURL(f.capture_id, "original", f.index);
      img.onload = draw;
      return {
        frame: f, n: num[f.index], img,
        yaw, pitch, roll,
        locked: !!f.manual_locked,
        ring: PhotoNames.ring(f),
        dirty: false,
      };
    });

    app.innerHTML = `
      <div class="screen">
        <div class="topbar">
          <button class="back" onclick="history.back()" title="Back">←</button>
          <h1>Place the photos by hand</h1>
        </div>
        <div class="container arrange-wrap">
          <div class="card">
            <div class="muted" id="hint">
              Every photo, whole and uncut, where it would be placed. Drag one to
              move it; click it to lock it there. A locked photo is built exactly
              where you put it — nothing will move it afterwards.
            </div>
            <div class="arrange-actions">
              <button id="lockAll">Lock all</button>
              <button id="unlockAll">Unlock all</button>
              <button id="resetOne" disabled>Reset this photo</button>
              <span class="muted" id="counts"></span>
            </div>
          </div>
          <div class="arrange-stage">
            <canvas id="map"></canvas>
          </div>
          <div class="card" id="selCard" hidden>
            <div id="selName" style="font-weight:600"></div>
            <div class="muted" id="selPos" style="margin:4px 0 10px"></div>
            <label class="muted" style="font-size:.85rem">Turn it upright
              <input type="range" id="rollRange" min="-180" max="180" step="1" value="0">
            </label>
          </div>
          <div class="card">
            <button class="primary" id="goBtn" style="width:100%">Build with these positions</button>
            <div class="muted" id="goNote" style="margin-top:8px;font-size:.85rem"></div>
          </div>
        </div>
      </div>`;

    const canvas = app.querySelector("#map");
    const ctx = canvas.getContext("2d");
    const counts = app.querySelector("#counts");
    const goBtn = app.querySelector("#goBtn");
    const goNote = app.querySelector("#goNote");
    const selCard = app.querySelector("#selCard");
    const selName = app.querySelector("#selName");
    const selPos = app.querySelector("#selPos");
    const rollRange = app.querySelector("#rollRange");
    const resetOne = app.querySelector("#resetOne");

    let selected = null;
    let dragging = null;

    function sizeCanvas() {
      const w = Math.min(canvas.parentElement.clientWidth, 1400);
      canvas.width = Math.max(320, Math.floor(w));
      canvas.height = Math.floor(canvas.width / 2);
      draw();
    }

    // Where a photo sits on the map, and how big it is drawn.
    function rectOf(it) {
      const w = canvas.width, h = canvas.height;
      const cx = ((it.yaw + Math.PI) / TAU) * w;
      const cy = ((it.pitch + Math.PI / 2) / Math.PI) * h;
      const ph = it.img.naturalHeight || 4, pw = it.img.naturalWidth || 3;
      const rw = (DEFAULT_HFOV / 360) * w;
      return { cx, cy, rw, rh: rw * (ph / pw) };
    }

    function draw() {
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "#0b0d12";
      ctx.fillRect(0, 0, w, h);

      // The horizon and the quarter turns, so a position can be read off the
      // map rather than guessed at.
      ctx.strokeStyle = "rgba(255,255,255,.14)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2);
      for (let k = 1; k < 4; k++) { ctx.moveTo((k / 4) * w, 0); ctx.lineTo((k / 4) * w, h); }
      ctx.stroke();

      items.forEach((it) => {
        const r = rectOf(it);
        // Drawn at both ends when it straddles the seam, so a photo near the
        // edge is never half missing.
        [0, -w, w].forEach((off) => {
          const cx = r.cx + off;
          if (cx + r.rw / 2 < 0 || cx - r.rw / 2 > w) return;
          ctx.save();
          ctx.translate(cx, r.cy);
          ctx.rotate(it.roll);
          ctx.globalAlpha = it.locked ? 1 : 0.72;
          if (it.img.complete && it.img.naturalWidth) {
            ctx.drawImage(it.img, -r.rw / 2, -r.rh / 2, r.rw, r.rh);
          } else {
            ctx.fillStyle = "#1e2430";
            ctx.fillRect(-r.rw / 2, -r.rh / 2, r.rw, r.rh);
          }
          ctx.globalAlpha = 1;
          ctx.lineWidth = it === selected ? 3 : 2;
          ctx.strokeStyle = it === selected ? "#ffd84d"
            : it.locked ? "#35d07f" : "rgba(255,255,255,.35)";
          ctx.strokeRect(-r.rw / 2, -r.rh / 2, r.rw, r.rh);
          ctx.restore();

          ctx.fillStyle = it.locked ? "#35d07f" : "#e7ecf3";
          ctx.font = "600 13px system-ui, sans-serif";
          ctx.textAlign = "center";
          ctx.fillText(`${it.locked ? "🔒 " : ""}Photo ${it.n}`, cx, r.cy - r.rh / 2 - 6);
        });
      });

      const locked = items.filter((i) => i.locked).length;
      counts.textContent = `${locked} of ${items.length} locked`;
      goNote.textContent = locked === 0
        ? "Nothing is locked yet, so the build will place every photo itself."
        : locked === items.length
          ? "Every photo is locked: the build will use exactly these positions."
          : `${locked} locked in place; the rest are placed by the build.`;
    }

    function hit(x, y) {
      const w = canvas.width;
      for (let i = items.length - 1; i >= 0; i--) {
        const it = items[i], r = rectOf(it);
        for (const off of [0, -w, w]) {
          const dx = x - (r.cx + off), dy = y - r.cy;
          const c = Math.cos(-it.roll), s = Math.sin(-it.roll);
          const lx = dx * c - dy * s, ly = dx * s + dy * c;
          if (Math.abs(lx) <= r.rw / 2 && Math.abs(ly) <= r.rh / 2) return it;
        }
      }
      return null;
    }

    function select(it) {
      selected = it;
      selCard.hidden = !it;
      resetOne.disabled = !it;
      if (it) {
        selName.textContent = `Photo ${it.n} — ${it.ring.label}`;
        rollRange.value = Math.round(it.roll * 180 / Math.PI);
        showPos(it);
      }
      draw();
    }

    function showPos(it) {
      selPos.textContent =
        `${Math.round(wrapYaw(it.yaw) * 180 / Math.PI)}° round, ` +
        `${Math.round(-it.pitch * 180 / Math.PI)}° up/down` +
        (it.locked ? " — locked" : "");
    }

    function pos(ev) {
      const r = canvas.getBoundingClientRect();
      const p = ev.touches ? ev.touches[0] : ev;
      return { x: (p.clientX - r.left) * canvas.width / r.width,
               y: (p.clientY - r.top) * canvas.height / r.height };
    }

    function onDown(ev) {
      const { x, y } = pos(ev);
      const it = hit(x, y);
      if (!it) { select(null); return; }
      select(it);
      dragging = { it, x, y, moved: false };
      ev.preventDefault();
    }

    function onMove(ev) {
      if (!dragging) return;
      const { x, y } = pos(ev);
      const dx = x - dragging.x, dy = y - dragging.y;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragging.moved = true;
      const it = dragging.it;
      it.yaw = wrapYaw(it.yaw + (dx / canvas.width) * TAU);
      it.pitch = Math.max(-Math.PI / 2, Math.min(Math.PI / 2,
        it.pitch + (dy / canvas.height) * Math.PI));
      it.dirty = true;
      dragging.x = x; dragging.y = y;
      showPos(it);
      draw();
      ev.preventDefault();
    }

    function onUp() {
      if (!dragging) return;
      // A click with no drag is the lock. Moving a photo does not lock it by
      // itself: you may want to nudge several before pinning any.
      if (!dragging.moved) {
        dragging.it.locked = !dragging.it.locked;
        dragging.it.dirty = true;
        showPos(dragging.it);
      }
      dragging = null;
      draw();
    }

    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    canvas.addEventListener("touchstart", onDown, { passive: false });
    canvas.addEventListener("touchmove", onMove, { passive: false });
    window.addEventListener("touchend", onUp);

    rollRange.addEventListener("input", () => {
      if (!selected) return;
      selected.roll = Number(rollRange.value) * Math.PI / 180;
      selected.dirty = true;
      draw();
    });

    resetOne.addEventListener("click", () => {
      if (!selected) return;
      const p = placed[selected.frame.index];
      if (p) { selected.yaw = p.yaw; selected.pitch = p.pitch; selected.roll = p.roll || 0; }
      selected.locked = false;
      selected.dirty = true;
      rollRange.value = Math.round(selected.roll * 180 / Math.PI);
      showPos(selected);
      draw();
    });

    app.querySelector("#lockAll").addEventListener("click", () => {
      items.forEach((it) => { it.locked = true; it.dirty = true; });
      draw();
    });
    app.querySelector("#unlockAll").addEventListener("click", () => {
      items.forEach((it) => { it.locked = false; it.dirty = true; });
      draw();
    });

    goBtn.addEventListener("click", async () => {
      goBtn.disabled = true;
      goBtn.textContent = "Saving positions…";
      try {
        // Every photo that was touched is saved, locked or not: the numbers
        // outlive the lock, so unlocking and locking again keeps the
        // arrangement instead of throwing it away.
        for (const it of items) {
          if (!it.dirty && !it.locked) continue;
          await OrbitAPI.setFramePose(captureId, it.frame.index, {
            yaw: it.yaw, pitch: it.pitch, roll: it.roll, locked: it.locked,
          });
        }
        goBtn.textContent = "Building…";
        await OrbitAPI.process(captureId);
        Router.navigate(`#/processing/${captureId}`);
      } catch (e) {
        goBtn.disabled = false;
        goBtn.textContent = "Build with these positions";
        goNote.textContent = "Could not start: " + e.message;
      }
    });

    const onResize = () => sizeCanvas();
    window.addEventListener("resize", onResize);
    sizeCanvas();

    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("touchend", onUp);
    };
  }

  return { mount };
})();
