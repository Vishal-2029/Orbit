const ScreenProcessing = (() => {
  async function mount(app, captureId) {
    let capture;
    try {
      const res = await OrbitAPI.getCapture(captureId);
      capture = res.capture;
    } catch (e) {
      app.innerHTML = `<div class="container"><div class="card"><h2>Not found</h2><p class="muted">${escapeHtml(e.message)}</p></div></div>`;
      return () => {};
    }

    app.innerHTML = `
      <div class="screen">
        <div class="topbar">
          <button class="back" onclick="location.hash='#/'" title="Back to home">←</button>
          <h1>Building your 360</h1>
        </div>
        <div class="container">
          <div id="banner"></div>
          <div class="card">
            <div class="muted" id="statusLine">Connecting…</div>
            <div class="progress-wrap"><div id="bar" class="progress-bar"></div></div>
            <div class="muted" id="countLine"></div>
            <div id="ticks" class="frame-ticks"></div>
          </div>
          <div class="card">
            <div class="muted" style="margin-bottom:6px">Live log</div>
            <div id="log" class="log"></div>
          </div>
          <div id="doneRow" style="display:none">
            <button class="primary" id="viewBtn" style="width:100%">View my 360 →</button>
          </div>
          <div id="retryRow" style="display:none">
            <button id="retryBtn" style="width:100%">Build it again</button>
            <div class="muted" style="margin-top:8px;font-size:.85rem">
              Uses the same photos. The previous result is discarded.
            </div>
          </div>
        </div>
      </div>`;

    const bar = app.querySelector("#bar");
    const statusLine = app.querySelector("#statusLine");
    const countLine = app.querySelector("#countLine");
    const ticks = app.querySelector("#ticks");
    const log = app.querySelector("#log");
    const banner = app.querySelector("#banner");
    const doneRow = app.querySelector("#doneRow");
    const viewBtn = app.querySelector("#viewBtn");
    const retryRow = app.querySelector("#retryRow");
    const retryBtn = app.querySelector("#retryBtn");

    // A fallback view or an outright failure is usually not the photos' fault -
    // a worker that ran out of memory produces both - so offer the same photos
    // to another run rather than making the user shoot the whole thing again.
    function offerRetry() {
      retryRow.style.display = "block";
      retryBtn.onclick = async () => {
        retryBtn.disabled = true;
        retryBtn.textContent = "Starting\u2026";
        try {
          await OrbitAPI.process(captureId);
          // Remount so this screen watches the new run from the start.
          Router.reload();
        } catch (e) {
          retryBtn.disabled = false;
          retryBtn.textContent = "Build it again";
          banner.innerHTML = `<div class="banner bad">Could not start again: ${escapeHtml(e.message)}</div>`;
        }
      };
    }

    const total = capture.frame_count || 0;
    const tickState = new Array(total).fill("pending");
    renderTicks();

    function renderTicks() {
      ticks.innerHTML = tickState.map((s) => `<div class="tick ${s}"></div>`).join("");
    }

    function logLine(text) {
      const div = document.createElement("div");
      div.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
      log.appendChild(div);
      log.scrollTop = log.scrollHeight;
    }

    function setProgress(pct, processed, tot) {
      bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
      countLine.textContent = tot ? `${processed} / ${tot} frames processed` : "";
    }

    function finish(manifest, degraded, degradedWhy) {
      statusLine.textContent = degraded ? "Done, with a fallback view" : "Done!";
      setProgress(100, total, total);
      tickState.fill("done");
      renderTicks();
      if (degraded) {
        banner.innerHTML = `<div class="banner warn">This 360 uses a fallback view: ${escapeHtml(degradedWhy || "the full stitch could not complete.")}</div>`;
      } else {
        banner.innerHTML = `<div class="banner good">Your 360 view is ready.</div>`;
      }
      doneRow.style.display = "block";
      viewBtn.onclick = () => Router.navigate(`#/view-id/${captureId}`);
      if (degraded) offerRetry();
    }

    function fail(message) {
      statusLine.textContent = "Processing failed";
      banner.innerHTML = `<div class="banner bad">${escapeHtml(message || "Something went wrong while building your 360.")}</div>`;
      offerRetry();
    }

    // If it's already done (e.g. user navigated back here), short-circuit.
    if (capture.status === "ready" || capture.status === "partial") {
      let manifest = null;
      try {
        manifest = await OrbitAPI.getManifest(captureId);
      } catch (_) {}
      finish(manifest, capture.status === "partial", manifest && manifest.degraded_why);
      return () => {};
    }
    if (capture.status === "failed") {
      fail(capture.error);
      return () => {};
    }

    statusLine.textContent = `Status: ${capture.status}`;
    setProgress(capture.processed_count ? capture.processed_count * 80 / total : 0, capture.processed_count, total);
    logLine(`Connected. Capture status: ${capture.status}.`);

    let closedCleanly = false;
    const ws = OrbitAPI.connectWS(captureId, {
      onOpen: () => logLine("WebSocket connected."),
      onEvent: (ev) => {
        if (ev.type === "status") {
          statusLine.textContent = `Status: ${ev.status}`;
          setProgress(ev.progress, ev.processed, ev.total);
          logLine(ev.message || `status: ${ev.status}`);
        } else if (ev.type === "frame_done") {
          const idx = ev.index ?? ev.processed - 1;
          if (idx != null && idx >= 0 && idx < tickState.length) tickState[idx] = "done";
          renderTicks();
          setProgress(ev.progress, ev.processed, ev.total);
          logLine(ev.message || `frame ${idx} done`);
        } else if (ev.type === "frame_failed") {
          const idx = ev.index;
          if (idx != null && idx >= 0 && idx < tickState.length) tickState[idx] = "failed";
          renderTicks();
          logLine("frame failed: " + (ev.message || "unknown reason"));
        } else if (ev.type === "ready") {
          closedCleanly = true;
          logLine(ev.message || "ready");
          finish(ev.manifest, ev.status === "partial" || (ev.manifest && ev.manifest.degraded), ev.manifest && ev.manifest.degraded_why);
        } else if (ev.type === "error") {
          closedCleanly = true;
          logLine("error: " + (ev.message || "unknown error"));
          fail(ev.message);
        }
      },
      onError: () => logLine("WebSocket error."),
      onClose: () => {
        logLine("WebSocket closed.");
        if (!closedCleanly) {
          // Poll once in case the socket dropped after the finishing event.
          OrbitAPI.getCapture(captureId).then(({ capture: c }) => {
            if (c.status === "ready" || c.status === "partial") {
              OrbitAPI.getManifest(captureId).then((m) =>
                finish(m, c.status === "partial", m && m.degraded_why)
              ).catch(() => finish(null, c.status === "partial"));
            } else if (c.status === "failed") {
              fail(c.error);
            }
          }).catch(() => {});
        }
      },
    });

    return () => {
      try { ws.close(); } catch (_) {}
    };
  }

  return { mount };
})();
