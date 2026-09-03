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
    const coverWarn = app.querySelector("#coverWarn");
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
    // The ring drawn on screen is the horizon row only - that is the circle the
    // user physically walks round. Ceiling and floor dots are not part of it.
    const ringSlots = state.slots.filter((s) => Math.abs(s.pitch || 0) < 20);
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
      drawYawGauge(ctx, view);
      drawPitchGauge(ctx, view);

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
        if (p.visible) {
          drawDot(ctx, p, nearest, false, true);
        } else {
          // Which way is shorter to turn?
          const inv = Orientation.qConjugate(q);
          const d = Orientation.qRotate(inv, dir);
          drawTurnHint(ctx, view, d[0] >= 0 ? 1 : -1, p.angle);
        }
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

    // Two gauges, top-left: one for turning left and right, one for looking up
    // and down. Coverage fails in both axes independently - a user can walk the
    // full circle and still never point at the ceiling - and a single ring can
    // only ever show one of them.
    //
    // They sit top-left, below the instruction panel and clear of the shutter
    // row. The instruction panel is centred, so the left edge is free.
    const GAUGE_R = 30;
    const GAUGE_TOP = 178;

    function gaugeCentres(view) {
      const x = GAUGE_R + 18;
      return {
        yaw:   { cx: x, cy: GAUGE_TOP },
        pitch: { cx: x, cy: GAUGE_TOP + GAUGE_R * 2 + 22 },
      };
    }

    // Where the phone points right now, or null before the reference frame is
    // established.
    function currentHeading() {
      if (!refFrame || !tracker.quaternion) return null;
      return SphereMath.headingOf(refFrame, tracker.quaternion);
    }

    function drawNeedle(c, cx, cy, angleRad, r) {
      c.strokeStyle = "#fff";
      c.lineWidth = 2;
      c.beginPath();
      c.moveTo(cx + Math.cos(angleRad) * (r - 12), cy + Math.sin(angleRad) * (r - 12));
      c.lineTo(cx + Math.cos(angleRad) * (r + 7), cy + Math.sin(angleRad) * (r + 7));
      c.stroke();
    }

    function gaugeLabel(c, cx, cy, done, total, caption) {
      c.fillStyle = "#fff";
      c.textAlign = "center";
      c.font = "700 15px system-ui, sans-serif";
      c.fillText(`${done}`, cx, cy - 1);
      c.font = "500 9px system-ui, sans-serif";
      c.fillStyle = "rgba(255,255,255,.7)";
      c.fillText(`of ${total}`, cx, cy + 11);
      c.font = "600 9px system-ui, sans-serif";
      c.fillStyle = "rgba(255,255,255,.85)";
      c.fillText(caption, cx, cy + GAUGE_R + 13);
    }

    // Turning: one segment per dot on the horizon ring, filled once shot. This
    // exists because of the commonest way a capture goes wrong - the user turns
    // most of the way, stops, and never notices a third of the room is missing.
    // A circle with a bite out of it reads in half a second; a list does not.
    function drawYawGauge(c, view) {
      const { cx, cy } = gaugeCentres(view).yaw;
      const arc = (Math.PI * 2) / Math.max(1, ringSlots.length);

      c.save();
      c.lineWidth = 8;
      c.lineCap = "butt";
      ringSlots.forEach((slot) => {
        // -90deg so "front" sits at the top, the way a compass reads.
        const a0 = (slot.yaw / 180) * Math.PI - Math.PI / 2 - arc / 2 + 0.03;
        c.strokeStyle = state.shots.has(slot.id)
          ? "rgba(61,220,132,.95)"     // photographed
          : "rgba(255,255,255,.22)";   // still missing
        c.beginPath();
        c.arc(cx, cy, GAUGE_R, a0, a0 + arc - 0.06);
        c.stroke();
      });

      const h = currentHeading();
      if (h) drawNeedle(c, cx, cy, (h.yaw / 180) * Math.PI - Math.PI / 2, GAUGE_R);

      const done = ringSlots.filter((sl) => state.shots.has(sl.id)).length;
      gaugeLabel(c, cx, cy, done, ringSlots.length, "L / R");
      c.restore();
    }

    // Looking up and down. The circle is split the way the world is: ceiling
    // across the top, floor across the bottom, the horizon down each side. The
    // needle rides the same mapping, so pointing the phone up swings it up.
    const PITCH_BANDS = [
      { id: "up",    from: -160, to: -20,  test: (p) => p >= 20 },
      { id: "down",  from: 20,   to: 160,  test: (p) => p <= -20 },
      { id: "level", from: -20,  to: 20,   test: (p) => Math.abs(p) < 20 },
      { id: "level2", from: 160, to: 200,  test: (p) => Math.abs(p) < 20 },
    ];

    function drawPitchGauge(c, view) {
      const { cx, cy } = gaugeCentres(view).pitch;
      const rad = (d) => (d / 180) * Math.PI;

      c.save();
      c.lineWidth = 8;
      c.lineCap = "butt";

      let done = 0, total = 0;
      PITCH_BANDS.forEach((band) => {
        const slots = state.slots.filter((sl) => band.test(sl.pitch || 0));
        const shot = slots.filter((sl) => state.shots.has(sl.id)).length;
        // level is drawn as two arcs; count it once.
        if (band.id !== "level2") { done += shot; total += slots.length; }

        const complete = slots.length > 0 && shot >= slots.length;
        c.strokeStyle = slots.length === 0
          ? "rgba(255,255,255,.10)"      // nothing planned in this band
          : complete
            ? "rgba(61,220,132,.95)"
            : shot > 0
              ? "rgba(255,210,122,.9)"   // started, not finished
              : "rgba(255,255,255,.22)";
        c.beginPath();
        c.arc(cx, cy, GAUGE_R, rad(band.from) + 0.04, rad(band.to) - 0.04);
        c.stroke();
      });

      const h = currentHeading();
      // pitch +90 (straight up) -> top of the dial, -90 (floor) -> bottom.
      if (h) drawNeedle(c, cx, cy, rad(-h.pitch), GAUGE_R);

      gaugeLabel(c, cx, cy, done, total, "Up / Dn");
      c.restore();
    }

    // A big arrow at the edge when the next dot is off screen. Users looked
    // straight past the small one.
    function drawTurnHint(c, view, side, degrees) {
      const y = view.height / 2;
      const x = side > 0 ? view.width - 46 : 46;
      c.save();
      c.translate(x, y);
      c.rotate(side > 0 ? 0 : Math.PI);
      c.fillStyle = "rgba(76,141,255,.95)";
      c.beginPath();
      c.moveTo(24, 0); c.lineTo(-14, 17); c.lineTo(-14, -17);
      c.closePath(); c.fill();
      c.restore();
      c.save();
      c.fillStyle = "#fff";
      c.font = "700 13px system-ui, sans-serif";
      c.textAlign = "center";
      c.fillText(`${Math.round(degrees)}°`, x, y + 38);
      c.restore();
    }

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

      // Say nothing while things are working. The old version announced
      // "Gyroscope tracking" at every moment, which told the user nothing they
      // could act on and buried the messages that mattered.
      if (!live) {
        statusPill.className = "status-pill off";
        statusPill.textContent = "No motion sensor — tap the button for each photo";
      } else if (!refFrame) {
        statusPill.className = "status-pill warn";
        statusPill.textContent = "Point at anything and take your first photo to begin";
      } else {
        statusPill.className = "status-pill hidden";
        statusPill.textContent = "";
      }
      setFrontBtn.hidden = !live || !refFrame;

      const t = liveTarget || nextSlot();
      const need = plan.min_required || 1;
      const ringLeft = ringSlots.filter((s) => !state.shots.has(s.id)).length;

      if (!t) {
        targetLabel.textContent = "All done";
        targetHint.textContent = "Tap Build my 360 below.";
      } else if (ringLeft > 0) {
        // While the circle is unfinished, that is the only thing worth saying.
        targetLabel.textContent = "Keep turning the same way";
        targetHint.textContent = ringLeft === 1
          ? "One more photo closes the circle."
          : `${ringLeft} more photos to close the circle.`;
      } else {
        targetLabel.textContent = "Circle complete";
        targetHint.textContent = state.shots.size < state.slots.length
          ? "Now point up at the ceiling and down at the floor."
          : "Tap Build my 360 below.";
      }
      void need;
    }

    function renderThumbs() {
      thumbStrip.innerHTML = state.slots
        .filter((s) => state.shots.has(s.id))
        .map((s) => `<img src="${state.shots.get(s.id).url}" alt="${escapeHtml(s.label)}" />`)
        .join("");
    }

    function updateFinishState() {
      const need = plan.min_required || 4;
      const ringLeft = ringSlots.filter((s) => !state.shots.has(s.id)).length;
      const short = Math.max(0, need - state.shots.size);

      // The circle has to be closed. Letting someone build a 360 with a hole in
      // it and only telling them afterwards is how this went wrong before.
      finishBtn.disabled = ringLeft > 0 || short > 0;
      finishBtn.textContent = ringLeft > 0
        ? `${ringLeft} more to close the circle`
        : "Build my 360 →";
      retakeBtn.disabled = state.shots.size === 0;

      // Closing the ring is not the same as covering the sphere. Anything above
      // or below that was never shot comes back as a soft blurred wash in the
      // finished 360, because there is no photograph of it - so say so here,
      // while it can still be fixed, rather than letting it be a surprise.
      const off = state.slots.filter((sl) => Math.abs(sl.pitch || 0) >= 20);
      const offLeft = off.filter((sl) => !state.shots.has(sl.id)).length;
      if (coverWarn) {
        if (ringLeft === 0 && offLeft > 0) {
          const pct = Math.round((1 - offLeft / state.slots.length) * 100);
          coverWarn.textContent =
            `${offLeft} dot${offLeft === 1 ? "" : "s"} above or below not shot ` +
            `— about ${pct}% covered. Those directions will look blurred.`;
          coverWarn.hidden = false;
        } else {
          coverWarn.hidden = true;
        }
      }
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
          </div>

          <!-- One short line. Big enough to read at arm's length, high enough
               that it never sits over the reticle. -->
          <div class="say-panel">
            <div id="targetLabel" class="say-big"></div>
            <div id="targetHint" class="say-small"></div>
          </div>

          <div id="statusPill" class="status-pill"></div>

          <div class="cam-foot">
            <div id="thumbStrip" class="thumb-strip"></div>
            <div class="cam-bottom">
              <button id="retakeBtn" class="side-btn" title="Undo last photo">↺</button>
              <button id="shutterBtn" class="shutter" aria-label="Take photo"></button>
              <button id="setFrontBtn" class="side-btn" title="Start the circle here" hidden>⌖</button>
            </div>
            <div id="camErr" class="cam-err"></div>
            <div id="coverWarn" class="cover-warn" hidden></div>
            <button id="finishBtn" class="primary finish-btn" disabled></button>
          </div>
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
