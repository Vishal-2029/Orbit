// Place the photos by hand, one at a time, starting at Photo 1.
//
// A solver works from what photos have in common, and some photos have too
// little to go on: a window of repeating mullions matches one bay to the next
// and means it; a blank wall matches nothing at all. When the machine cannot
// tell, the person looking at the picture can.
//
// The first version of this screen drew all 32 photos at once. It was accurate
// and unreadable - overlapping frames, labels colliding, no sense of where to
// start. So the screen now works the way the capture did: one photo at a time,
// in order, with its own ring behind it for context and the rest out of the
// way. Photo 1, place it, lock it, next.
//
// The map is equirectangular: left to right is all the way round, top to bottom
// is straight up to straight down. A photo is drawn as a rectangle of its own
// angular size, which is honest near the horizon and increasingly generous
// towards the poles - the same distortion the finished 360 has, and the reason
// the ceiling shot looks so wide.
const ScreenArrange = (() => {
  // Assumed width of one photo, in degrees, when nothing better is known. Only
  // affects how big a photo is DRAWN here; the build measures the real field of
  // view from the photos themselves.
  const DEFAULT_HFOV = 63;
  const TAU = Math.PI * 2;

  const wrapYaw = (y) => ((y + Math.PI) % TAU + TAU) % TAU - Math.PI;
  const deg = (r) => Math.round(r * 180 / Math.PI);

  async function mount(app, captureId) {
    let capture, frames, manifest = null;
    try {
      const [cap, fr] = await Promise.all([
        OrbitAPI.getCapture(captureId),
        OrbitAPI.listFrames(captureId),
      ]);
      capture = cap.capture;
      frames = (fr.frames || []).filter((f) => !f.excluded);
      // Where the last build put each photo. Absent on a capture never built,
      // and on one built before positions were recorded - the sensor's own
      // angles stand in, which is what the build would have started from.
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

    // In shot order, which is the order the numbers run in: Photo 1 first.
    const items = frames.map((f) => {
      const p = placed[f.index];
      const manual = f.manual_yaw != null && f.manual_pitch != null;
      return {
        frame: f,
        n: num[f.index],
        ring: PhotoNames.ring(f),
        yaw: manual ? f.manual_yaw
          : p ? p.yaw : wrapYaw((Number(f.yaw) || 0) * Math.PI / 180),
        pitch: manual ? f.manual_pitch
          : p ? p.pitch : -(Number(f.pitch) || 0) * Math.PI / 180,
        roll: manual ? (f.manual_roll || 0) : (p ? (p.roll || 0) : 0),
        locked: !!f.manual_locked,
        dirty: false,
        img: null,
      };
    }).sort((a, b) => a.n - b.n);

    items.forEach((it) => {
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.src = OrbitAPI.imageURL(it.frame.capture_id, "original", it.frame.index);
      img.onload = draw;
      it.img = img;
    });

    let at = 0;                 // which photo is in focus - Photo 1 to start
    let context = "ring";       // what else to show: "ring" | "all" | "none"
    let dragging = null;

    app.innerHTML = `
      <div class="screen">
        <div class="topbar">
          <button class="back" onclick="history.back()" title="Back">←</button>
          <h1>Place the photos by hand</h1>
        </div>
        <div class="container arrange-wrap">

          <div class="card arrange-now">
            <div class="arrange-now-head">
              <div>
                <div id="nowName" style="font-weight:700;font-size:1.05rem"></div>
                <div class="muted" id="nowPos" style="font-size:.85rem;margin-top:2px"></div>
              </div>
              <div class="arrange-step">
                <button id="prevBtn" title="Previous photo">‹</button>
                <span class="muted" id="stepCount"></span>
                <button id="nextBtn" title="Next photo">›</button>
              </div>
            </div>
            <div class="arrange-actions">
              <button class="primary" id="lockNext">Lock and next</button>
              <button id="unlockBtn">Unlock</button>
              <button id="resetBtn">Reset to where the build put it</button>
            </div>
            <label class="muted arrange-roll">Turn it upright
              <input type="range" id="rollRange" min="-180" max="180" step="1" value="0">
            </label>
          </div>

          <div class="arrange-stage">
            <canvas id="map"></canvas>
          </div>

          <div class="card">
            <div class="arrange-show">
              <span class="muted">Show behind it:</span>
              <button class="chip" data-show="ring">its ring</button>
              <button class="chip" data-show="all">everything</button>
              <button class="chip" data-show="none">nothing</button>
              <span class="muted" id="counts" style="margin-left:auto"></span>
            </div>
            <div class="filmstrip" id="strip"></div>
          </div>

          <div class="card">
            <button class="primary" id="goBtn" style="width:100%">Build with these positions</button>
            <div class="muted" id="goNote" style="margin-top:8px;font-size:.85rem"></div>
          </div>
        </div>
      </div>`;

    const canvas = app.querySelector("#map");
    const ctx = canvas.getContext("2d");
    const strip = app.querySelector("#strip");
    const counts = app.querySelector("#counts");
    const goBtn = app.querySelector("#goBtn");
    const goNote = app.querySelector("#goNote");
    const nowName = app.querySelector("#nowName");
    const nowPos = app.querySelector("#nowPos");
    const stepCount = app.querySelector("#stepCount");
    const rollRange = app.querySelector("#rollRange");

    const current = () => items[at];

    function rectOf(it) {
      const w = canvas.width, h = canvas.height;
      const ph = (it.img && it.img.naturalHeight) || 4;
      const pw = (it.img && it.img.naturalWidth) || 3;
      const rw = (DEFAULT_HFOV / 360) * w;
      return {
        cx: ((wrapYaw(it.yaw) + Math.PI) / TAU) * w,
        cy: ((it.pitch + Math.PI / 2) / Math.PI) * h,
        rw, rh: rw * (ph / pw),
      };
    }

    function visible(it) {
      if (it === current()) return true;
      if (context === "all") return true;
      if (context === "ring") return it.ring.id === current().ring.id;
      return false;
    }

    function drawOne(it, focused) {
      const w = canvas.width;
      const r = rectOf(it);
      // Drawn at both ends when it straddles the seam, so a photo near the edge
      // is never half missing.
      [0, -w, w].forEach((off) => {
        const cx = r.cx + off;
        if (cx + r.rw / 2 < -20 || cx - r.rw / 2 > w + 20) return;
        ctx.save();
        ctx.translate(cx, r.cy);
        ctx.rotate(it.roll);
        ctx.globalAlpha = focused ? 1 : 0.22;
        if (it.img && it.img.complete && it.img.naturalWidth) {
          ctx.drawImage(it.img, -r.rw / 2, -r.rh / 2, r.rw, r.rh);
        } else {
          ctx.fillStyle = "#1e2430";
          ctx.fillRect(-r.rw / 2, -r.rh / 2, r.rw, r.rh);
        }
        ctx.globalAlpha = 1;
        ctx.lineWidth = focused ? 3 : 1;
        ctx.strokeStyle = focused ? "#ffd84d"
          : it.locked ? "rgba(53,208,127,.55)" : "rgba(255,255,255,.25)";
        ctx.strokeRect(-r.rw / 2, -r.rh / 2, r.rw, r.rh);
        ctx.restore();

        // Only the photo in focus is named on the map. Thirty-two labels at
        // once was the unreadable part; the filmstrip below carries the rest.
        if (focused) {
          const label = `${it.locked ? "🔒 " : ""}Photo ${it.n}`;
          ctx.font = "700 15px system-ui, sans-serif";
          ctx.textAlign = "center";
          const tw = ctx.measureText(label).width + 14;
          const ty = Math.max(12, r.cy - r.rh / 2 - 22);
          ctx.fillStyle = "rgba(10,12,18,.85)";
          ctx.fillRect(cx - tw / 2, ty, tw, 22);
          ctx.fillStyle = it.locked ? "#35d07f" : "#ffd84d";
          ctx.fillText(label, cx, ty + 16);
        }
      });
    }

    function draw() {
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "#0b0d12";
      ctx.fillRect(0, 0, w, h);

      // The horizon, the quarter turns, and which way each is - so a position
      // can be read off the map rather than guessed at.
      ctx.strokeStyle = "rgba(255,255,255,.14)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2);
      for (let k = 1; k < 4; k++) { ctx.moveTo((k / 4) * w, 0); ctx.lineTo((k / 4) * w, h); }
      ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,.35)";
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "center";
      [["behind", 0], ["left", 0.25], ["ahead", 0.5], ["right", 0.75], ["behind", 1]]
        .forEach(([name, f]) => ctx.fillText(name, f * w, h / 2 - 8));
      ctx.textAlign = "left";
      ctx.fillText("straight up", 6, 14);
      ctx.fillText("straight down", 6, h - 6);

      items.forEach((it) => { if (it !== current() && visible(it)) drawOne(it, false); });
      drawOne(current(), true);

      const locked = items.filter((i) => i.locked).length;
      counts.textContent = `${locked} of ${items.length} locked`;
      goNote.textContent = locked === 0
        ? "Nothing is locked yet, so the build will place every photo itself."
        : locked === items.length
          ? "Every photo is locked: the build will use exactly these positions."
          : `${locked} locked in place; the rest are placed by the build.`;
    }

    function renderNow() {
      const it = current();
      nowName.textContent = `Photo ${it.n} — ${it.ring.label}`;
      nowPos.textContent =
        `${deg(wrapYaw(it.yaw))}° round, ${-deg(it.pitch)}° up/down` +
        (it.locked ? " · locked" : " · not locked");
      stepCount.textContent = `${at + 1} / ${items.length}`;
      rollRange.value = deg(it.roll);
      app.querySelectorAll(".chip").forEach((c) =>
        c.classList.toggle("on", c.dataset.show === context));
      renderStrip();
      draw();
    }

    function renderStrip() {
      strip.innerHTML = items.map((it, i) => `
        <button class="film ${i === at ? "on" : ""} ${it.locked ? "locked" : ""}"
                data-i="${i}" title="Photo ${it.n} — ${escapeHtml(it.ring.label)}">
          <img src="${OrbitAPI.imageURL(it.frame.capture_id, "thumb", it.frame.index)}"
               onerror="this.style.visibility='hidden'" alt="">
          <span>${it.locked ? "🔒" : ""}${it.n}</span>
        </button>`).join("");
      strip.querySelectorAll(".film").forEach((b) => {
        b.addEventListener("click", () => { at = Number(b.dataset.i); renderNow(); });
      });
      const on = strip.querySelector(".film.on");
      if (on) on.scrollIntoView({ block: "nearest", inline: "center" });
    }

    function sizeCanvas() {
      const w = Math.min(canvas.parentElement.clientWidth, 1400);
      canvas.width = Math.max(320, Math.floor(w));
      canvas.height = Math.floor(canvas.width / 2);
      draw();
    }

    function pos(ev) {
      const r = canvas.getBoundingClientRect();
      const p = ev.touches ? ev.touches[0] : ev;
      return { x: (p.clientX - r.left) * canvas.width / r.width,
               y: (p.clientY - r.top) * canvas.height / r.height };
    }

    // Dragging moves the photo in focus, wherever on the map you take hold:
    // the photo being placed is the subject, so the whole canvas is its handle.
    // Tapping another photo in view switches to it instead.
    function onDown(ev) {
      const { x, y } = pos(ev);
      const r = rectOf(current());
      const w = canvas.width;
      const insideCurrent = [0, -w, w].some((off) =>
        Math.abs(x - (r.cx + off)) <= r.rw / 2 && Math.abs(y - r.cy) <= r.rh / 2);
      if (!insideCurrent) {
        for (let i = items.length - 1; i >= 0; i--) {
          if (!visible(items[i]) || items[i] === current()) continue;
          const q = rectOf(items[i]);
          if ([0, -w, w].some((off) =>
            Math.abs(x - (q.cx + off)) <= q.rw / 2 && Math.abs(y - q.cy) <= q.rh / 2)) {
            at = i; renderNow(); return;
          }
        }
      }
      dragging = { x, y };
      ev.preventDefault();
    }

    function onMove(ev) {
      if (!dragging) return;
      const { x, y } = pos(ev);
      const it = current();
      it.yaw = wrapYaw(it.yaw + ((x - dragging.x) / canvas.width) * TAU);
      it.pitch = Math.max(-Math.PI / 2, Math.min(Math.PI / 2,
        it.pitch + ((y - dragging.y) / canvas.height) * Math.PI));
      it.dirty = true;
      dragging = { x, y };
      nowPos.textContent = `${deg(wrapYaw(it.yaw))}° round, ${-deg(it.pitch)}° up/down` +
        (it.locked ? " · locked" : " · not locked");
      draw();
      ev.preventDefault();
    }

    const onUp = () => { dragging = null; };

    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    canvas.addEventListener("touchstart", onDown, { passive: false });
    canvas.addEventListener("touchmove", onMove, { passive: false });
    window.addEventListener("touchend", onUp);

    function step(d) {
      at = Math.max(0, Math.min(items.length - 1, at + d));
      renderNow();
    }
    app.querySelector("#prevBtn").addEventListener("click", () => step(-1));
    app.querySelector("#nextBtn").addEventListener("click", () => step(1));

    app.querySelector("#lockNext").addEventListener("click", () => {
      const it = current();
      it.locked = true;
      it.dirty = true;
      if (at < items.length - 1) step(1); else renderNow();
    });
    app.querySelector("#unlockBtn").addEventListener("click", () => {
      current().locked = false;
      current().dirty = true;
      renderNow();
    });
    app.querySelector("#resetBtn").addEventListener("click", () => {
      const it = current();
      const p = placed[it.frame.index];
      if (p) { it.yaw = p.yaw; it.pitch = p.pitch; it.roll = p.roll || 0; }
      it.locked = false;
      it.dirty = true;
      renderNow();
    });
    rollRange.addEventListener("input", () => {
      current().roll = Number(rollRange.value) * Math.PI / 180;
      current().dirty = true;
      draw();
    });
    app.querySelectorAll(".chip").forEach((c) => {
      c.addEventListener("click", () => { context = c.dataset.show; renderNow(); });
    });

    // Arrow keys nudge by a degree, which a drag cannot do precisely.
    function onKey(e) {
      const it = current();
      const stepRad = Math.PI / 180;
      if (e.key === "ArrowLeft") it.yaw = wrapYaw(it.yaw - stepRad);
      else if (e.key === "ArrowRight") it.yaw = wrapYaw(it.yaw + stepRad);
      else if (e.key === "ArrowUp") it.pitch = Math.max(-Math.PI / 2, it.pitch - stepRad);
      else if (e.key === "ArrowDown") it.pitch = Math.min(Math.PI / 2, it.pitch + stepRad);
      else if (e.key === "[") step(-1);
      else if (e.key === "]") step(1);
      else return;
      it.dirty = true;
      e.preventDefault();
      renderNow();
    }
    window.addEventListener("keydown", onKey);

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
    renderNow();

    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("touchend", onUp);
      window.removeEventListener("keydown", onKey);
    };
  }

  return { mount };
})();
