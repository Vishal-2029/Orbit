// Blue-dot capture, modelled on how Street View's photo-sphere capture works.
//
// Instead of telling the user "turn right 40 degrees", each target is drawn as
// a dot floating at a fixed direction in the world. The user moves the phone so
// a dot falls inside the centre reticle, holds still, and the shutter fires by
// itself. Turning the phone slides the dots across the screen because their
// positions are recomputed from the live gyroscope rotation every frame.
const ScreenCapture = (() => {
  const AUTO_HOLD_MS = 550;      // steady time inside the reticle before firing
  const RETICLE_DEG = 8;         // how close to centre counts as "on target"

  async function mount(app, params) {
    // The router hands screens a bare id string; accept an object too so this
    // cannot silently become the string "undefined" again.
    const captureId = typeof params === "string" ? params : (params && params.id);
    if (!captureId) {
      app.innerHTML = `<div class="container"><div class="card">
        <h2>No capture selected</h2>
        <p class="muted">That link is missing a capture id.</p>
        <button class="primary" onclick="location.hash='#/'">Back home</button></div></div>`;
      return () => {};
    }

    let capture, plan;
    try {
      const res = await OrbitAPI.getCapture(captureId);
      capture = res.capture;
      plan = await OrbitAPI.getPlan(captureId);
    } catch (e) {
      app.innerHTML = `<div class="container"><div class="card"><h2>Could not open this capture</h2>
        <p class="muted">${escapeHtml(e.message)}</p>
        <button class="primary" onclick="location.hash='#/'">Back home</button></div></div>`;
      return () => {};
    }

    if (!window.isSecureContext) {
      app.innerHTML = insecureHtml();
      return () => {};
    }

    const state = {
      slots: (plan.slots || []).map((s) => ({ ...s })),
      shots: new Map(),      // slot id -> { blob, url, index, yaw, pitch, quat }
      stream: null,
      tilt: null,
    };

    app.innerHTML = shellHtml(capture, plan, state.slots.length);
    const video = app.querySelector("#camVideo");
    const dotCanvas = app.querySelector("#dotCanvas");
    const ghost = app.querySelector("#ghostImg");
    const shutter = app.querySelector("#shutterBtn");
    const targetLabel = app.querySelector("#targetLabel");
    const targetHint = app.querySelector("#targetHint");
    const counter = app.querySelector("#counter");
    const statusPill = app.querySelector("#statusPill");
    const thumbStrip = app.querySelector("#thumbStrip");
    const finishBtn = app.querySelector("#finishBtn");
    const retakeBtn = app.querySelector("#retakeBtn");
    const setFrontBtn = app.querySelector("#setFrontBtn");
    const gridBtn = app.querySelector("#gridBtn");
    const camGrid = app.querySelector("#camGrid");
    const levelBubble = app.querySelector("#levelBubble");
    const errBox = app.querySelector("#camErr");
    const ctx = dotCanvas.getContext("2d");

    // --- camera ---
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 } },
        audio: false,
      });
      video.srcObject = state.stream;
      await video.play().catch(() => {});
    } catch (e) {
      errBox.textContent = "Camera access failed: " + e.message;
      errBox.classList.add("warn");
    }

    // --- orientation ---
    const tracker = Orientation.create();
    let refFrame = null;        // reference frame, locked when Front is taken
    let sensorMode = "none";

    async function startTracking() {
      sensorMode = await tracker.start();
      renderStatus();
    }
    // Chrome needs no gesture; iOS does. Try immediately, and again on any tap.
    startTracking();
    app.addEventListener("click", function once() {
      if (!tracker.isLive()) startTracking();
      else app.removeEventListener("click", once);
    }, { once: false });

    // --- grid ---
    let gridOn = true;
    try {
      const saved = localStorage.getItem("orbit.grid");
      if (saved !== null) gridOn = saved === "1";
    } catch (_) {}
    function applyGrid() {
      camGrid.hidden = !gridOn;
      gridBtn.classList.toggle("active", gridOn);
      gridBtn.setAttribute("aria-pressed", gridOn ? "true" : "false");
    }
    gridBtn.addEventListener("click", () => {
      gridOn = !gridOn;
      try { localStorage.setItem("orbit.grid", gridOn ? "1" : "0"); } catch (_) {}
      applyGrid();
    });
    applyGrid();

    setFrontBtn.addEventListener("click", () => {
      const q = tracker.quaternion;
      if (!q) return;
      refFrame = SphereMath.referenceFrame(q);
      errBox.classList.remove("warn");
      errBox.textContent = "Front is now where you are pointing.";
      setTimeout(() => { if (errBox.textContent.startsWith("Front is now")) errBox.textContent = ""; }, 2500);
    });

    // --- the render loop ---
    // Everything visible is recomputed per frame from the live rotation, so the
    // dots track the phone rather than being redrawn on discrete events.
    let raf = 0;
    let liveTarget = null;   // the dot nearest the centre right now
    let holdSince = 0;
    let holdSlotId = null;
    let firing = false;

    function nextSlot() {
      return state.slots.find((s) => !state.shots.has(s.id)) || null;
    }

    function fitCanvas() {
      const r = video.getBoundingClientRect();
      const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
      if (dotCanvas.width !== w || dotCanvas.height !== h) {
        dotCanvas.width = w; dotCanvas.height = h;
      }
      return { width: w, height: h, hfov: 65 };
    }

    function frameLoop() {
      raf = requestAnimationFrame(frameLoop);
      const view = fitCanvas();
      ctx.clearRect(0, 0, view.width, view.height);

      const q = tracker.quaternion;
      const live = tracker.isLive();

      // Level bubble comes straight from the rotation, no separate sensor.
      if (live && q) {
        const f = SphereMath.forwardOf(q);
        state.tilt = Math.asin(Math.max(-1, Math.min(1, f[2]))) * 180 / Math.PI;
        updateLevel();
      }

      drawReticle(ctx, view, live);

      if (!live || !q) { renderStatus(); return; }
      if (!refFrame) { renderStatus(); return; }

      let onTarget = null;

      // With a full sphere of dots, drawing all of them at once is noise. Only
      // those near where the phone is pointing are shown - the rest still
      // exist, they are simply out of view, exactly like Street View capture.
      const VISIBLE_DEG = 55;
      let nearest = null;
      let nearestAngle = 1e9;

      for (const slot of state.slots) {
        const taken = state.shots.has(slot.id);
        const dir = SphereMath.directionFor(refFrame, slot.yaw, slot.pitch || 0);
        const p = SphereMath.project(q, dir, view);

        if (!taken && p.angle < nearestAngle) {
          nearestAngle = p.angle;
          nearest = slot;
        }
        if (!p.visible || p.angle > VISIBLE_DEG) continue;
        drawDot(ctx, p, slot, taken, false);
      }

      // Whatever unshot dot is closest to the centre becomes the live target,
      // so the user points wherever they like rather than being marched
      // through a fixed list.
      liveTarget = nearest;
      if (nearest) {
        const dir = SphereMath.directionFor(refFrame, nearest.yaw, nearest.pitch || 0);
        const p = SphereMath.project(q, dir, view);
        if (p.visible) drawDot(ctx, p, nearest, false, true);
        else drawEdgeArrow(ctx, view, q, dir, nearest);
        if (p.angle <= RETICLE_DEG) onTarget = nearest;
      }

      // Auto-shoot: the dot must sit inside the reticle for a moment, so we
      // never fire mid-swing and get a blurred frame.
      if (onTarget && !firing) {
        if (holdSlotId !== onTarget.id) { holdSlotId = onTarget.id; holdSince = Date.now(); }
        const held = Date.now() - holdSince;
        drawHoldRing(ctx, view, Math.min(1, held / AUTO_HOLD_MS));
        shutter.classList.add("aligned");
        if (held >= AUTO_HOLD_MS) { holdSlotId = null; takeShot(onTarget); }
      } else {
        holdSlotId = null;
        shutter.classList.toggle("aligned", false);
      }
      renderStatus();
    }

    // --- drawing ---
    function drawReticle(c, view, live) {
      const cx = view.width / 2, cy = view.height / 2;
      const r = Math.min(view.width, view.height) * 0.13;
      c.save();
      c.strokeStyle = live ? "rgba(255,255,255,.85)" : "rgba(255,255,255,.3)";
      c.lineWidth = 3;
      c.beginPath(); c.arc(cx, cy, r, 0, Math.PI * 2); c.stroke();
      c.restore();
    }

    function drawHoldRing(c, view, progress) {
      const cx = view.width / 2, cy = view.height / 2;
      const r = Math.min(view.width, view.height) * 0.13;
      c.save();
      c.strokeStyle = "#3ddc84";
      c.lineWidth = 6;
      c.lineCap = "round";
      c.beginPath();
      c.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + progress * Math.PI * 2);
      c.stroke();
      c.restore();
    }

    function drawDot(c, p, slot, taken, isTarget) {
      const r = isTarget ? 26 : 16;
      c.save();
      if (taken) {
        c.fillStyle = "rgba(61,220,132,.85)";
        c.strokeStyle = "rgba(255,255,255,.9)";
      } else if (isTarget) {
        c.fillStyle = "rgba(76,141,255,.92)";
        c.strokeStyle = "#fff";
      } else {
        c.fillStyle = "rgba(76,141,255,.35)";
        c.strokeStyle = "rgba(255,255,255,.45)";
      }
      c.lineWidth = isTarget ? 3 : 2;
      c.beginPath(); c.arc(p.x, p.y, r, 0, Math.PI * 2); c.fill(); c.stroke();
      if (taken) {
        c.strokeStyle = "#0b2a17"; c.lineWidth = 3; c.lineCap = "round";
        c.beginPath();
        c.moveTo(p.x - r * 0.4, p.y);
        c.lineTo(p.x - r * 0.1, p.y + r * 0.35);
        c.lineTo(p.x + r * 0.45, p.y - r * 0.35);
        c.stroke();
      }
      if (isTarget) {
        c.fillStyle = "rgba(0,0,0,.65)";
        c.font = "600 13px system-ui, sans-serif";
        c.textAlign = "center";
        const label = slot.label || "";
        const w = c.measureText(label).width + 14;
        c.fillRect(p.x - w / 2, p.y + r + 6, w, 20);
        c.fillStyle = "#fff";
        c.fillText(label, p.x, p.y + r + 20);
      }
      c.restore();
    }

    // When the target is behind the camera there is nothing to draw in place,
    // so an arrow on the edge points the shortest way round to it.
    function drawEdgeArrow(c, view, q, dir, slot) {
      const inv = Orientation.qConjugate(q);
      const d = Orientation.qRotate(inv, dir);
      const cx = view.width / 2, cy = view.height / 2;
      const ang = Math.atan2(-d[1], d[0]);       // screen-space bearing
      const rad = Math.min(view.width, view.height) * 0.34;
      const x = cx + Math.cos(ang) * rad, y = cy + Math.sin(ang) * rad;
      c.save();
      c.translate(x, y); c.rotate(ang);
      c.fillStyle = "rgba(76,141,255,.95)";
      c.beginPath(); c.moveTo(20, 0); c.lineTo(-12, 13); c.lineTo(-12, -13); c.closePath(); c.fill();
      c.restore();
      c.save();
      c.fillStyle = "#fff";
      c.font = "600 13px system-ui, sans-serif";
      c.textAlign = "center";
      c.fillText(slot.label || "", x, y + 34);
      c.restore();
    }

    function updateLevel() {
      if (!levelBubble || state.tilt == null) return;
      const t = Math.max(-30, Math.min(30, state.tilt));
      levelBubble.style.transform = `translate(-50%, ${(-t * 2.2).toFixed(1)}px)`;
      const level = Math.abs(state.tilt) <= 5;
      levelBubble.classList.toggle("level", level);
    }

    // --- status + targets ---
    function renderStatus() {
      const live = tracker.isLive();
      const label = { gyro: "Gyroscope", absolute: "Compass + gyro", deviceorientation: "Basic sensor" }[tracker.source];

      if (!live) {
        statusPill.className = "status-pill off";
        statusPill.textContent = "No motion sensor — tap the shutter for each shot";
      } else if (!refFrame) {
        statusPill.className = "status-pill warn";
        statusPill.textContent = `${label} ready — point at your starting view and tap Set Front`;
      } else {
        statusPill.className = "status-pill on";
        const n = state.shots.size;
        statusPill.textContent = `${label} tracking · ${n} of ${state.slots.length} captured`;
      }
      setFrontBtn.hidden = !live;

      const t = liveTarget || nextSlot();
      const left = state.slots.length - state.shots.size;
      targetLabel.textContent = t ? t.label : "Every dot captured";
      targetHint.textContent = t
        ? (t.hint || "")
        : "Tap Build my 360 when you are ready.";
      if (t && left > 0) targetHint.textContent += "  (" + left + " dots left)";
      counter.textContent = `${state.shots.size} of ${state.slots.length}`;
    }

    function renderThumbs() {
      thumbStrip.innerHTML = state.slots
        .filter((s) => state.shots.has(s.id))
        .map((s) => `<img src="${state.shots.get(s.id).url}" alt="${escapeHtml(s.label)}" />`)
        .join("");
    }

    function updateFinishState() {
      const n = state.shots.size;
      const need = plan.min_required || 4;
      finishBtn.disabled = n < need;
      finishBtn.textContent = n < need
        ? `${need - n} more to go`
        : `Build my 360 →`;
      retakeBtn.disabled = n === 0;
    }

    function updateGhost() {
      const shot = [...state.shots.values()].pop();
      if (shot) { ghost.src = shot.url; ghost.style.display = "block"; }
      else ghost.style.display = "none";
    }

    // --- capture ---
    function grabFrame() {
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth || 1280;
      canvas.height = video.videoHeight || 720;
      canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
      return new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.9));
    }

    async function takeShot(slot) {
      if (firing) return;
      firing = true;
      shutter.disabled = true;
      flash();
      try {
        const blob = await grabFrame();
        const q = tracker.quaternion;

        // Record where the phone ACTUALLY pointed, not the angle we asked for.
        // This is the data the stitcher will eventually use as a starting pose.
        let yaw = slot.yaw, pitch = slot.pitch || 0, quat = null;
        if (q && refFrame && tracker.isLive()) {
          const h = SphereMath.headingOf(refFrame, q);
          yaw = h.yaw; pitch = h.pitch;
          quat = q;
        }

        await OrbitAPI.uploadPhoto(captureId, {
          blob, index: slot.index, slotId: slot.id,
          yaw, pitch, hasHeading: quat != null, quat,
          source: quat != null ? tracker.source : "none",
        });

        const url = URL.createObjectURL(blob);
        state.shots.set(slot.id, { blob, url, index: slot.index, yaw, pitch, quat });
        errBox.textContent = "";
        errBox.classList.remove("warn");
        renderThumbs(); updateFinishState(); updateGhost(); renderStatus();
      } catch (e) {
        if (e.code === "duplicate_direction") {
          errBox.classList.add("warn");
          errBox.innerHTML = `<strong>Same direction as before.</strong><br>${escapeHtml(e.message)}`;
        } else {
          errBox.classList.remove("warn");
          errBox.textContent = "Could not save that photo: " + e.message;
        }
      } finally {
        firing = false;
        shutter.disabled = false;
      }
    }

    function flash() {
      const f = app.querySelector("#flash");
      if (!f) return;
      f.classList.remove("go");
      void f.offsetWidth;   // restart the animation
      f.classList.add("go");
    }

    shutter.addEventListener("click", () => {
      const t = liveTarget || nextSlot();
      if (!t) return;
      // The first manual shot also establishes Front if it is not set yet.
      if (!refFrame && tracker.quaternion) refFrame = SphereMath.referenceFrame(tracker.quaternion);
      takeShot(t);
    });

    retakeBtn.addEventListener("click", () => {
      const last = [...state.shots.keys()].pop();
      if (!last) return;
      URL.revokeObjectURL(state.shots.get(last).url);
      state.shots.delete(last);
      renderThumbs(); updateFinishState(); updateGhost(); renderStatus();
    });

    finishBtn.addEventListener("click", async () => {
      finishBtn.disabled = true;
      finishBtn.textContent = "Starting…";
      try {
        await OrbitAPI.process(captureId);
        Router.navigate(`#/processing/${captureId}`);
      } catch (e) {
        errBox.classList.remove("warn");
        errBox.textContent = e.message;
        updateFinishState();
      }
    });

    renderStatus(); updateFinishState(); renderThumbs();
    raf = requestAnimationFrame(frameLoop);

    return () => {
      cancelAnimationFrame(raf);
      tracker.stop();
      if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
      state.shots.forEach((s) => URL.revokeObjectURL(s.url));
    };
  }

  function shellHtml(capture, plan, slotCount) {
    return `
      <div class="capture-screen">
        <video id="camVideo" playsinline muted autoplay></video>
        <img id="ghostImg" style="display:none" />
        <div id="camGrid" class="cam-grid" hidden>
          <span class="v v1"></span><span class="v v2"></span>
          <span class="h h1"></span><span class="h h2"></span>
          <span class="horizon"></span>
          <span id="levelBubble" class="level-bubble"></span>
        </div>
        <canvas id="dotCanvas" class="dot-canvas"></canvas>
        <div id="flash" class="shot-flash"></div>
        <div class="cam-overlay">
          <div class="cam-top">
            <button class="back" onclick="location.hash='#/'" title="Back to home">←</button>
            <div class="cam-title">${escapeHtml(capture.title)}</div>
            <button id="gridBtn" class="side-btn grid-btn" title="Framing grid" aria-pressed="false">⊞</button>
            <span id="counter" class="counter">0 of ${slotCount}</span>
          </div>
          <div id="statusPill" class="status-pill"></div>
          <div class="target-panel">
            <div id="targetLabel" class="target-label"></div>
            <div id="targetHint" class="target-hint"></div>
          </div>
          <div id="camErr" class="cam-err"></div>
          <div id="thumbStrip" class="thumb-strip"></div>
          <div class="cam-bottom">
            <button id="retakeBtn" class="side-btn" title="Undo last shot">↺</button>
            <button id="shutterBtn" class="shutter" aria-label="Take photo"></button>
            <button id="setFrontBtn" class="side-btn" title="Set this direction as Front" hidden>⌖</button>
          </div>
          <button id="finishBtn" class="primary finish-btn" disabled></button>
        </div>
      </div>`;
  }

  function insecureHtml() {
    return `<div class="container"><div class="card">
      <h2>The camera needs a secure connection</h2>
      <p class="muted">Browsers only allow camera access over <b>https</b> or on <b>localhost</b>.
      You are on <code>${escapeHtml(location.origin)}</code>, which is neither.</p>
      <p class="muted">On Android, open <code>chrome://flags/#unsafely-treat-insecure-origin-as-secure</code>,
      add <code>${escapeHtml(location.origin)}</code>, set it to Enabled and relaunch Chrome.</p>
      <button class="primary" onclick="location.hash='#/'">Back home</button>
    </div></div>`;
  }

  return { mount };
})();
