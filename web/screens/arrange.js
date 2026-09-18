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

    // Step by step, the way the capture was shot: the level ring first, then
    // tilted up, then tilted down, then the ceiling and floor, and finally
    // everything together. A ring is a decision on its own - its photos only
    // have to agree with each other - and the last step is where the rings are
    // checked against one another.
    const stages = (() => {
      const present = PhotoNames.byRing(frames).map((g) => g.ring);
      const steps = present
        .filter((r) => r.id !== "up" && r.id !== "down")
        .map((r) => ({ id: r.id, label: r.label, hint: r.hint,
                       match: (it) => it.ring.id === r.id }));
      const poles = present.filter((r) => r.id === "up" || r.id === "down");
      if (poles.length) {
        steps.push({ id: "poles", label: "Ceiling and floor", hint: "straight up and down",
                     match: (it) => it.ring.id === "up" || it.ring.id === "down" });
      }
      steps.push({ id: "all", label: "Everything together", hint: "check the rings against each other",
                   match: () => true });
      return steps;
    })();

    let stageAt = 0;
    let at = 0;                 // which photo is in focus - Photo 1 to start
    let context = "ring";       // what else to show: "ring" | "all" | "none"
    let view = "map";           // "map" (flat) or "model" (the frames in 3D)
    let model = null;
    let dragging = null;

    const stage = () => stages[stageAt];
    const inStage = () => items.filter((it) => stage().match(it));

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

          <div class="card arrange-steps">
            <div class="arrange-show">
              <span class="muted">Step:</span>
              <span id="stageChips"></span>
              <span class="muted" id="stageDone" style="margin-left:auto"></span>
            </div>
            <div class="arrange-actions" style="margin-top:10px">
              <button id="prevStage">Previous step</button>
              <button id="nextStage">Next step ›</button>
              <button id="lockStage">Lock this whole step</button>
            </div>
          </div>

          <div class="arrange-show" style="margin-bottom:8px">
            <span class="muted">View:</span>
            <button class="chip" data-view="map">flat map</button>
            <button class="chip" data-view="model">360 model</button>
            <span class="muted" style="font-size:.82rem">the photo frames where they sit, seen from the middle</span>
          </div>

          <div class="arrange-stage" id="stage">
            <div class="stage-bar">
              <div class="stage-step-group">
                <button id="prevTop" title="Previous photo">‹</button>
                <span id="countTop"></span>
                <button id="nextTop" title="Next photo">›</button>
              </div>
              <span class="stage-now" id="nowTop"></span>
              <button id="lockTop" class="stage-lock">Lock</button>
              <button id="fsExit" hidden>Close</button>
            </div>
            <canvas id="map"></canvas>
            <div id="model3d" class="arrange-3d" hidden></div>
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

    // `at` counts within the current step, so "3 / 12" is 3 of this ring.
    const current = () => inStage()[Math.min(at, inStage().length - 1)] || items[0];

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
      if (context === "none") return false;
      if (context === "all") return true;
      // "its ring" means this step's photos, which on the last step is all of
      // them - that step exists precisely to check the rings against each other.
      return stage().match(it);
    }

    // Which part this photo plays right now. Fixing a photo means judging it
    // against the one before and the one after it, so those two are named and
    // coloured rather than left in the crowd.
    function roleOf(it) {
      const list = inStage();
      if (it === current()) return "focus";
      if (it === list[at - 1]) return "prev";
      if (it === list[at + 1]) return "next";
      return "other";
    }

    const ROLE = {
      focus: { edge: "#ffd84d", alpha: 1, label: (n) => `Photo ${n}` },
      prev: { edge: "#5b8cff", alpha: 0.6, label: (n) => `‹ Photo ${n}` },
      next: { edge: "#ff9f43", alpha: 0.6, label: (n) => `Photo ${n} ›` },
      other: { edge: "rgba(255,255,255,.25)", alpha: 0.18, label: null },
    };

    function drawOne(it, role) {
      const focused = role === "focus";
      const look = ROLE[role];
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
        ctx.globalAlpha = look.alpha;
        if (it.img && it.img.complete && it.img.naturalWidth) {
          ctx.drawImage(it.img, -r.rw / 2, -r.rh / 2, r.rw, r.rh);
        } else {
          ctx.fillStyle = "#1e2430";
          ctx.fillRect(-r.rw / 2, -r.rh / 2, r.rw, r.rh);
        }
        ctx.globalAlpha = 1;
        ctx.lineWidth = focused ? 3 : role === "other" ? 1 : 2;
        ctx.strokeStyle = role === "other" && it.locked
          ? "rgba(53,208,127,.55)" : look.edge;
        ctx.strokeRect(-r.rw / 2, -r.rh / 2, r.rw, r.rh);
        ctx.restore();

        // The photo in hand and its two neighbours are named; the rest are
        // not. Thirty-two labels at once was the unreadable part, and these
        // three are the ones a fix is judged against.
        if (look.label) {
          const label = `${it.locked ? "🔒 " : ""}${look.label(it.n)}`;
          ctx.font = "700 15px system-ui, sans-serif";
          ctx.textAlign = "center";
          const tw = ctx.measureText(label).width + 14;
          const ty = Math.max(12, r.cy - r.rh / 2 - 22);
          ctx.fillStyle = "rgba(10,12,18,.85)";
          ctx.fillRect(cx - tw / 2, ty, tw, 22);
          ctx.fillStyle = it.locked && focused ? "#35d07f" : look.edge;
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

      // Drawn back to front: the crowd, then the neighbours, then the photo in
      // hand on top of everything.
      const order = { other: 0, prev: 1, next: 1, focus: 2 };
      items.filter(visible)
        .map((it) => [it, roleOf(it)])
        .sort((a, b) => order[a[1]] - order[b[1]])
        .forEach(([it, role]) => drawOne(it, role));
      if (model) model.refresh();

      const locked = items.filter((i) => i.locked).length;
      counts.textContent = `${locked} of ${items.length} locked`;
      goNote.textContent = locked === 0
        ? "Nothing is locked yet, so the build will place every photo itself."
        : locked === items.length
          ? "Every photo is locked: the build will use exactly these positions."
          : `${locked} locked in place; the rest are placed by the build.`;
    }

    function renderStages() {
      const chips = app.querySelector("#stageChips");
      chips.innerHTML = stages.map((st, i) => {
        const mine = items.filter(st.match);
        const done = mine.filter((m) => m.locked).length;
        return `<button class="chip ${i === stageAt ? "on" : ""}" data-stage="${i}"
                  title="${escapeHtml(st.hint || "")}">${i + 1}. ${escapeHtml(st.label)}${
                  done === mine.length && mine.length ? " \u2713" : ""}</button>`;
      }).join(" ");
      chips.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
        stageAt = Number(c.dataset.stage); at = 0; renderNow();
      }));
      const mine = inStage();
      const done = mine.filter((m) => m.locked).length;
      app.querySelector("#stageDone").textContent =
        `${stage().label}: ${done} of ${mine.length} locked`;
      app.querySelector("#prevStage").disabled = stageAt === 0;
      app.querySelector("#nextStage").disabled = stageAt === stages.length - 1;
    }

    function renderNow() {
      const it = current();
      nowName.textContent = `Photo ${it.n} — ${it.ring.label}`;
      nowPos.textContent =
        `${deg(wrapYaw(it.yaw))}° round, ${-deg(it.pitch)}° up/down` +
        (it.locked ? " · locked" : " · not locked");
      const posText = `${Math.min(at + 1, inStage().length)} / ${inStage().length}`;
      stepCount.textContent = posText;
      app.querySelector("#countTop").textContent = posText;
      app.querySelector("#nowTop").textContent =
        `Photo ${it.n} · ${it.ring.label}${it.locked ? " · locked" : ""}`;
      app.querySelector("#lockTop").textContent = it.locked ? "Unlock" : "Lock";
      rollRange.value = deg(it.roll);
      app.querySelectorAll(".chip[data-show]").forEach((c) =>
        c.classList.toggle("on", c.dataset.show === context));
      app.querySelectorAll(".chip[data-view]").forEach((c) =>
        c.classList.toggle("on", c.dataset.view === view));
      renderStages();
      renderStrip();
      draw();
    }

    function renderStrip() {
      strip.innerHTML = inStage().map((it, i) => `
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
      at = Math.max(0, Math.min(inStage().length - 1, at + d));
      renderNow();
      if (model && view === "model") model.lookAtItem(current());
    }
    app.querySelector("#prevBtn").addEventListener("click", () => step(-1));
    app.querySelector("#nextBtn").addEventListener("click", () => step(1));
    // The same stepper over the picture, because that is where the eyes are.
    app.querySelector("#prevTop").addEventListener("click", () => step(-1));
    app.querySelector("#nextTop").addEventListener("click", () => step(1));
    app.querySelector("#lockTop").addEventListener("click", () => {
      current().locked = !current().locked;
      current().dirty = true;
      renderNow();
    });

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
    app.querySelectorAll(".chip[data-show]").forEach((c) => {
      c.addEventListener("click", () => { context = c.dataset.show; renderNow(); });
    });

    // Stepping through the rings: one ring is a decision on its own, and the
    // last step is where they are checked against each other.
    function goStage(i) {
      stageAt = Math.max(0, Math.min(stages.length - 1, i));
      at = 0;
      renderNow();
      if (model && view === "model") model.lookAtItem(current());
    }
    app.querySelector("#prevStage").addEventListener("click", () => goStage(stageAt - 1));
    app.querySelector("#nextStage").addEventListener("click", () => goStage(stageAt + 1));
    app.querySelector("#lockStage").addEventListener("click", () => {
      inStage().forEach((it) => { it.locked = true; it.dirty = true; });
      renderNow();
    });

    // The flat map and the 3D model are two views of the same arrangement, so
    // switching between them changes nothing but how it is looked at.
    const modelHost = app.querySelector("#model3d");
    const stageEl = app.querySelector("#stage");
    const fsExit = app.querySelector("#fsExit");

    function setView(next) {
      view = next;
      canvas.hidden = view !== "map";
      modelHost.hidden = view !== "model";
      // The model is a room to look around, not a panel to peer into - on a
      // phone especially - so it takes the whole screen and gives it back.
      stageEl.classList.toggle("fullscreen", view === "model");
      fsExit.hidden = view !== "model";
      document.body.classList.toggle("no-scroll", view === "model");
      if (view === "model") {
        if (!model) {
          model = Arrange3D.create(modelHost, items, {
            roleOf,
            isVisible: (it) => visible(it),
            isFocused: (it) => it === current(),
            onSelect: (it) => {
              const i = inStage().indexOf(it);
              if (i >= 0) { at = i; renderNow(); }
            },
            // Dragging the photo in hand moves it here too: the model is where
            // a position is judged, so it has to be where it can be fixed.
            onMove: (it, yaw, pitch) => {
              it.yaw = yaw; it.pitch = pitch; it.dirty = true;
              nowPos.textContent = `${deg(wrapYaw(it.yaw))}° round, ${-deg(it.pitch)}° up/down` +
                (it.locked ? " · locked" : " · not locked");
              draw();
            },
          });
          if (!model) {
            modelHost.innerHTML = `<div class="muted" style="padding:20px">
              The 3D view needs WebGL, which this browser will not give us.</div>`;
          }
        }
        if (model) { model.resize(); model.refresh(); model.lookAtItem(current()); }
      }
      renderNow();
    }
    app.querySelectorAll(".chip[data-view]").forEach((c) => {
      c.addEventListener("click", () => setView(c.dataset.view));
    });
    fsExit.addEventListener("click", () => setView("map"));

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
      else if (e.key === "Escape" && view === "model") { setView("map"); return; }
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

    const onResize = () => { sizeCanvas(); if (model) model.resize(); };
    window.addEventListener("resize", onResize);
    sizeCanvas();
    renderNow();

    return () => {
      document.body.classList.remove("no-scroll");
      if (model) model.destroy();
      window.removeEventListener("resize", onResize);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("touchend", onUp);
      window.removeEventListener("keydown", onKey);
    };
  }

  return { mount };
})();
