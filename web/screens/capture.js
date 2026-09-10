// Blue-dot capture, modelled on how Street View's photo-sphere capture works.
//
// Instead of telling the user "turn right 40 degrees", each target is drawn as
// a dot floating at a fixed direction in the world. The user moves the phone so
// a dot falls inside the centre reticle and presses the shutter. The shutter
// turns green as soon as the dot is centred, so the press is a confirmation
// rather than a guess. Turning the phone slides the dots across the screen
// because their positions are recomputed from the live gyroscope rotation
// every frame.
//
// Auto-shoot - firing on its own after a short hold - is available behind the
// A button, but it is not the default.
const ScreenCapture = (() => {
  const AUTO_HOLD_MS = 550;      // steady time inside the reticle before firing
  // The same figure the server plans with. If they ever disagree the ghost
  // strip is the wrong width, which is worse than no ghost at all.
  const CAMERA_HFOV = 65;
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
    const counter = app.querySelector("#counter");
    const statusPill = app.querySelector("#statusPill");
    const thumbStrip = app.querySelector("#thumbStrip");
    const finishBtn = app.querySelector("#finishBtn");
    const coverWarn = app.querySelector("#coverWarn");
    const retakeBtn = app.querySelector("#retakeBtn");
    const setFrontBtn = app.querySelector("#setFrontBtn");
    const gridBtn = app.querySelector("#gridBtn");
    const autoBtn = app.querySelector("#autoBtn");
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

    // Hold one exposure and one white balance for the whole ring.
    //
    // This is the single most visible thing that separates a home-made 360 from
    // a real one. A phone re-meters every shot: point at a window and it stops
    // down, point at a wall and it opens up, and each photo arrives at a
    // different brightness and a different colour. Measured on a finished
    // panorama, the sky - which is one continuous thing in the real world -
    // swung 78 levels out of 255 between neighbouring photos, where anything
    // over about 4 is visible. That is the vertical banding.
    //
    // The stitcher already tries to correct it, but it can only apply one gain
    // per photo. That cannot undo a colour shift, and it cannot recover a
    // window that was blown out in one frame and correctly exposed in the next.
    // The fix has to happen here, before the photo is taken.
    //
    // Locked a moment AFTER the camera starts, deliberately: locking instantly
    // freezes whatever the sensor guessed in its first frame, which is usually
    // too dark. Letting it settle first locks something reasonable.
    //
    // Android Chrome supports this. iOS Safari does not expose the controls at
    // all, and some Android cameras only offer some of them - so every step is
    // attempted separately and a refusal is not an error. Without it the
    // capture still works exactly as it does today.
    let exposureLocked = false;

    // Fraction of the frame that is clipped, and how bright the brightest
    // sample was, over everything seen since the camera opened. Sampled from a
    // deliberately tiny canvas: this is a question about the scene as a whole,
    // and 48x48 answers it as well as a megapixel would for a fraction of the
    // cost on a phone.
    const metering = { clipped: 0, peak: 0, samples: 0 };
    const meterCanvas = document.createElement("canvas");
    meterCanvas.width = meterCanvas.height = 48;

    function sampleScene() {
      if (!video.videoWidth) return;
      try {
        const c = meterCanvas.getContext("2d", { willReadFrequently: true });
        c.drawImage(video, 0, 0, meterCanvas.width, meterCanvas.height);
        const px = c.getImageData(0, 0, meterCanvas.width, meterCanvas.height).data;
        let clipped = 0, peak = 0;
        for (let i = 0; i < px.length; i += 4) {
          // Rec. 601 luma - close enough, and it is what the sensor meters on.
          const y = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
          if (y >= 250) clipped++;
          if (y > peak) peak = y;
        }
        metering.clipped = Math.max(metering.clipped, clipped / (px.length / 4));
        metering.peak = Math.max(metering.peak, peak);
        metering.samples++;
      } catch (_) {
        // A cross-origin or not-yet-ready video throws here. Nothing to do.
      }
    }

    // Cleared by the teardown returned at the bottom of this screen, so it
    // does not keep sampling a stopped video after the user navigates away.
    const meterTimer = setInterval(sampleScene, 250);

    function exposureBiasFor(caps) {
      const range = caps.exposureCompensation;
      if (!range || typeof range.min !== "number" || typeof range.max !== "number") {
        return null;
      }
      // Under a fiftieth of the frame clipped is a lamp or a reflection, which
      // is normal and not worth darkening the whole capture for.
      if (metering.samples < 2 || metering.clipped < 0.02) return null;

      // One stop for a blown patch, two for a wall of window. The units of
      // exposureCompensation are EV on every implementation that reports a
      // range, but the range itself varies, so the ask is clamped to it and
      // then to the step the camera actually quantises to.
      const stops = metering.clipped > 0.10 ? -2 : -1;
      const step = range.step || 0.1;
      let value = Math.max(range.min, Math.min(range.max, stops));
      value = Math.round(value / step) * step;
      return value === 0 ? null : value;
    }

    async function lockExposure() {
      const track = state.stream && state.stream.getVideoTracks()[0];
      if (!track || !track.getCapabilities) return false;

      let caps = {};
      try {
        caps = track.getCapabilities() || {};
      } catch (_) {
        return false;
      }

      // Ask for each mode only where the camera lists "manual" as supported.
      // Requesting an unsupported mode rejects the whole applyConstraints call,
      // which would lose the ones that WOULD have worked.
      const wanted = [];
      const supports = (name, value) =>
        Array.isArray(caps[name]) && caps[name].indexOf(value) !== -1;

      // Bias the exposure DOWN before freezing it, when the scene has
      // highlights the sensor cannot hold.
      //
      // Locking solved the banding, but it locks whatever the camera was
      // metering a second after it opened - which is a wall, because that is
      // what you are facing when you start. Meter for a wall indoors and every
      // window clips to pure white, and clipped is gone: no exposure
      // compensation in the stitcher can recover a pixel recorded as 255. The
      // opposite mistake is cheap, because a dark interior still holds its
      // detail and multi-band blending lifts it back.
      //
      // So the scene is sampled while the user is still aiming, and if the
      // brightest thing seen is blowing out, the lock is taken a stop or two
      // down. Cameras that do not offer the control simply skip this.
      const bias = exposureBiasFor(caps);
      if (bias !== null) wanted.push({ exposureCompensation: bias });
      if (supports("exposureMode", "manual")) wanted.push({ exposureMode: "manual" });
      if (supports("whiteBalanceMode", "manual")) wanted.push({ whiteBalanceMode: "manual" });
      // Focus too. A refocus between shots changes the framing slightly, which
      // the stitcher then has to absorb as a geometry error.
      if (supports("focusMode", "manual")) wanted.push({ focusMode: "manual" });

      const locked = [];
      for (const constraint of wanted) {
        const name = Object.keys(constraint)[0];
        try {
          await track.applyConstraints({ advanced: [constraint] });
          locked.push(name);
        } catch (_) {
          // This camera advertised the mode but would not take it. Carry on:
          // locking two of the three is still better than locking none.
        }
      }
      exposureLocked = locked.length > 0;
      return locked;
    }

    // Give the sensor a moment to meter the scene before freezing it.
    //
    // Nothing is shown on screen either way. When it works there is nothing to
    // say, and when it does not - an iPhone, mostly - there is nothing the user
    // can do about it, so a warning would only be noise during the one part of
    // this app where the screen is already busy. The console line is for
    // whoever is debugging a banded panorama later.
    // Later than it was: the sampler needs a few frames to have seen anything,
    // and the exposure it locks is only as good as what it has been shown. Two
    // and a half seconds is still well inside the time it takes somebody to
    // read the first instruction and line up the first dot.
    setTimeout(() => {
      lockExposure().then((locked) => {
        // Name what actually locked, not what was asked for. Some cameras take
        // exposure but refuse white balance, and a line claiming all three
        // would send the next person debugging a banded panorama the wrong way.
        console.log(locked.length
          ? "[orbit] locked for this capture: " + locked.join(", ")
          : "[orbit] this camera will not lock exposure; brightness may vary "
            + "between photos, which shows as vertical bands in the panorama");
        console.log("[orbit] metered %d frames, peak luma %d, %s%% clipped",
                    metering.samples, Math.round(metering.peak),
                    (metering.clipped * 100).toFixed(1));
      });
    }, 2500);

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

    // --- auto-shoot ---
    //
    // OFF by default: you line the dot up, then press the button yourself.
    //
    // Firing on its own reads as the app taking the photo out of your hands -
    // it goes off while you are still framing, and a shot you did not ask for
    // is one you then have to undo. Manual costs one tap and the shutter is
    // already green by the time you make it.
    //
    // Auto is still there behind the A button for anyone who wants to walk a
    // ring without touching the screen; the choice is remembered.
    let autoOn = false;
    try {
      const saved = localStorage.getItem("orbit.autoShoot");
      if (saved !== null) autoOn = saved === "1";
    } catch (_) {}
    function applyAuto() {
      autoBtn.classList.toggle("active", autoOn);
      autoBtn.setAttribute("aria-pressed", autoOn ? "true" : "false");
      autoBtn.title = autoOn
        ? "Auto: shoots when a dot is centred"
        : "Manual: tap the shutter yourself";
    }
    autoBtn.addEventListener("click", () => {
      autoOn = !autoOn;
      // Drop a countdown already in progress, so switching to manual mid-hold
      // cannot fire one last shot. (Not in applyAuto: that also runs at mount,
      // before holdSlotId is declared.)
      holdSlotId = null;
      try { localStorage.setItem("orbit.autoShoot", autoOn ? "1" : "0"); } catch (_) {}
      applyAuto();
    });
    applyAuto();

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
      // Turning towards a different dot changes which edge is shared and by how
      // much, so the strip has to follow. Only on an actual change: this runs
      // every frame.
      if (nearest !== liveTarget) {
        liveTarget = nearest;
        updateGhost();
      }
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
      //
      // In manual mode the green shutter still says "you are on target", but
      // the countdown ring is not drawn - a ring that fills and then does
      // nothing reads as a broken auto-shoot rather than a deliberate choice.
      if (onTarget && !firing) {
        if (holdSlotId !== onTarget.id) { holdSlotId = onTarget.id; holdSince = Date.now(); }
        const held = Date.now() - holdSince;
        if (autoOn) drawHoldRing(ctx, view, Math.min(1, held / AUTO_HOLD_MS));
        shutter.classList.add("aligned");
        if (autoOn && held >= AUTO_HOLD_MS) { holdSlotId = null; takeShot(onTarget); }
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
    const GAUGE_TOP = 108;

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
        // Count only what this gauge is about. The horizon belongs to the L/R
        // gauge, and including it here read as "2 of 32" while every shot above
        // and below was still missing.
        if (band.id === "up" || band.id === "down") { done += shot; total += slots.length; }

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

      // What to do next is already on screen three times over: the coverage
      // note under the button says what is left, each dot carries its own
      // label, and the two gauges show what is missing in each axis. A
      // full-width banner saying it a fourth time only covered the gauges up.
      void t;
      void need;
      void ringLeft;
    }

    function renderThumbs() {
      thumbStrip.innerHTML = state.slots
        .filter((s) => state.shots.has(s.id))
        .map((s) => `<img src="${state.shots.get(s.id).url}" alt="${escapeHtml(s.label)}" />`)
        .join("");
    }

    function updateFinishState() {
      const ringLeft = ringSlots.filter((s) => !state.shots.has(s.id)).length;

      // One photo is enough to build from. The plan is a target, not a gate:
      // it used to disable this button until the circle was closed and every
      // ring filled, which left anyone who wanted a partial 360 - or who
      // simply could not finish the turn - holding a set of photos they were
      // not allowed to do anything with. What is still missing is said below,
      // in the coverage note, where it is advice rather than a refusal.
      finishBtn.disabled = state.shots.size === 0;
      finishBtn.textContent =
        state.shots.size === 0 ? "Take a photo to start" : "Build my 360 →";
      retakeBtn.disabled = state.shots.size === 0;

      // What is left to shoot, said once, in one place. Anything never
      // photographed comes back as a soft blurred wash in the finished 360,
      // because there is no photograph of it - so say so here, while it can
      // still be fixed, rather than letting it be a surprise. It is the whole
      // of the guidance now that the button no longer withholds anything.
      const missing = state.slots.filter((sl) => !state.shots.has(sl.id)).length;
      if (coverWarn) {
        if (state.shots.size > 0 && missing > 0) {
          const pct = Math.round((1 - missing / state.slots.length) * 100);
          const ring = ringLeft > 0
            ? `${ringLeft} more to close the circle. `
            : "";
          coverWarn.textContent =
            `${ring}About ${pct}% covered — you can build now, but the ` +
            `${missing} direction${missing === 1 ? "" : "s"} not shot will look blurred.`;
          coverWarn.hidden = false;
        } else {
          coverWarn.hidden = true;
        }
      }
    }

    // Show only the STRIP of the last photo that should overlap this one.
    //
    // The ghost used to be the whole previous photo, faint, across the whole
    // viewfinder. That does not help you line anything up: the part of it
    // covering the middle of the screen shows a direction you have already
    // turned away from, so it is just haze over the thing you are aiming at,
    // and at the opacity needed to stay out of the way it was too faint to
    // align against anyway.
    //
    // What actually matters is one edge. Turning right, the LEFT edge of the
    // new frame shows the same wall as the RIGHT edge of the last one. Put
    // those side by side and a few degrees of pitch error - the small up-and-
    // down drift that leaves steps along a roofline - is obvious while it can
    // still be fixed.
    //
    // So: clip the ghost to its trailing edge, slide that strip to the leading
    // edge of the screen, and show it at an opacity you can actually judge
    // against.
    function updateGhost() {
      const shot = [...state.shots.values()].pop();
      if (!shot) {
        ghost.style.display = "none";
        return;
      }

      // How far the phone actually has to travel between that photo and the
      // next one, measured - not assumed.
      //
      // This used to use plan.yaw_step, one number for the whole capture. It
      // is wrong most of the time. The rings above and below the horizon step
      // WIDER, because a circle of latitude is shorter - 43 degrees at the
      // equator becomes 61 at 45 degrees up - so they share far less frame.
      // And slots can be shot in any order, so the last photo is often not the
      // neighbour of the next one at all.
      //
      // The result was a strip claiming an overlap that was not there: the
      // dashed line and the blue dot disagreed, and lining up against one put
      // the photo in the wrong place for the other.
      // The dot the user is aiming at, not the next one in the list. They are
      // free to point anywhere - liveTarget is whichever unshot dot is nearest
      // the centre - so measuring against the list order would describe a turn
      // they are not making.
      const target = liveTarget || nextSlot();
      if (!target) {
        ghost.style.display = "none";
        return;
      }

      // Shortest way round, and signed: which side the ghost belongs on.
      let dYaw = ((target.yaw - shot.yaw + 540) % 360) - 180;
      const dPitch = (target.pitch || 0) - (shot.pitch || 0);

      // A photo from a different ring does not share a clean vertical strip
      // with this one - the common region is a corner, not an edge - so there
      // is nothing honest to draw. Same for two slots that are simply too far
      // apart to overlap.
      // How much YAW one photo covers, which is not the field of view except
      // at the horizon. Tilt up and the same angular width spans more
      // longitude, because a circle of latitude is shorter - at 45 degrees up
      // a 65 degree lens covers 92 degrees of yaw. That is exactly why the
      // plan steps those rings wider, and ignoring it here made the upper and
      // lower rings look like they shared almost nothing when they share the
      // same third as the horizon does.
      //
      // Clamped, because cos goes to zero at the pole and the true answer
      // there is "all of it".
      const meanPitch = ((shot.pitch || 0) + (target.pitch || 0)) / 2;
      const yawSpan = Math.min(360,
        CAMERA_HFOV / Math.max(0.25, Math.cos(meanPitch * Math.PI / 180)));

      const vfov = CAMERA_HFOV * 4 / 3;
      const overlap = (yawSpan - Math.abs(dYaw)) / yawSpan;
      if (Math.abs(dPitch) > vfov * 0.35 || overlap < 0.12) {
        ghost.style.display = "none";
        return;
      }

      const keep = (100 - overlap * 100).toFixed(1);   // the part clipped away

      // The image stays full width and full height - it has to, or object-fit
      // would crop it and the strip would no longer be the edge it claims to
      // be. clip-path hides all but the overlapping edge, and the transform
      // slides what is left to the opposite side of the screen, which is where
      // that same view will appear in the shot about to be taken.
      //
      // Turning RIGHT means the target is at a greater yaw, so the shared view
      // leaves by the left of the frame - the ghost's right edge belongs at the
      // screen's left. Taken from the measured direction rather than from the
      // capture's nominal setting, because a user shooting out of order can be
      // going either way.
      if (dYaw > 0) {
        ghost.style.clipPath = `inset(0 0 0 ${keep}%)`;
        ghost.style.transform = `translateX(-${keep}%)`;
        ghost.dataset.side = "left";
      } else {
        ghost.style.clipPath = `inset(0 ${keep}% 0 0)`;
        ghost.style.transform = `translateX(${keep}%)`;
        ghost.dataset.side = "right";
      }
      ghost.src = shot.url;
      ghost.style.display = "block";
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

        const saved = await OrbitAPI.uploadPhoto(captureId, {
          blob, index: slot.index, slotId: slot.id,
          yaw, pitch, hasHeading: quat != null, quat,
          source: quat != null ? tracker.source : "none",
        });

        const url = URL.createObjectURL(blob);
        state.shots.set(slot.id, { blob, url, index: slot.index, yaw, pitch, quat });
        // Shooting the same direction twice is worth mentioning - it spends a
        // shot without buying any coverage - but the photo is kept, so this is
        // a note beside a saved photo, not a rejection. It used to come back as
        // a 409 that threw the shot away.
        if (saved && saved.warning) {
          errBox.classList.add("warn");
          errBox.innerHTML =
            `<strong>Same direction as before.</strong><br>${escapeHtml(saved.warning)}`;
        } else {
          errBox.textContent = "";
          errBox.classList.remove("warn");
        }
        renderThumbs(); updateFinishState(); updateGhost(); renderStatus();
      } catch (e) {
        errBox.classList.remove("warn");
        errBox.textContent = "Could not save that photo: " + e.message;
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
      clearInterval(meterTimer);
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
            <button id="autoBtn" class="side-btn auto-btn" title="Shoot automatically when a dot is centred" aria-pressed="false">A</button>
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
