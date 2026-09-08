const ScreenHome = (() => {
  async function mount(app) {
    let mode = "sphere";
    app.innerHTML = `
      <div class="screen">
        <div class="topbar"><h1>Orbit — 360 Capture</h1></div>
        <div class="container">
          <div class="card">
            <div class="field">
              <label>Title</label>
              <input id="titleInput" placeholder="Living room, my desk, the car..." maxlength="80" />
            </div>
            <div class="field">
              <label>Mode</label>
              <div class="mode-row">
                <button class="mode-btn selected" data-mode="sphere">
                  <div class="title">🌐 360 view</div>
                  <div class="desc">Stand still and turn. Dots guide you all the way round, plus ceiling and floor.</div>
                </button>
                <button class="mode-btn" data-mode="spin">
                  <div class="title">🔄 Object spin</div>
                  <div class="desc">Turntable — rotate an object in front of a fixed camera, from three heights.</div>
                </button>
              </div>
            </div>
            <button id="startBtn" class="primary" style="width:100%">Start capture</button>
            <div id="errBox" class="muted" style="margin-top:8px;color:var(--bad)"></div>
          </div>

          <!-- Somebody who already HAS a 360 should not have to reshoot it here.
               A phone's Photo Sphere mode and any 360 camera both produce a
               finished equirectangular image, and either beats a handheld ring
               of stills. This publishes one straight to a share link. -->
          <div class="card upload-card">
            <div class="upload-head">
              <div>
                <strong>Already have a 360 photo?</strong>
                <p class="muted">Upload one from a 360 camera, or from your phone's
                   Photo Sphere mode, and get a share link straight away.</p>
              </div>
            </div>
            <input id="panoFile" type="file" accept="image/jpeg,image/png" hidden />
            <button id="panoBtn" style="width:100%">Choose a 360 photo</button>
            <div id="panoErr" class="upload-err" hidden></div>
          </div>

          <h3 class="muted" style="margin:22px 0 8px">Previous captures</h3>
          <div id="captureList" class="capture-list"><p class="muted">Loading…</p></div>
        </div>
      </div>
    `;

    const startBtn = app.querySelector("#startBtn");

    app.querySelectorAll(".mode-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        mode = btn.dataset.mode;
        app.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("selected", b === btn));
      });
    });

    startBtn.addEventListener("click", async () => {
      const errBox = app.querySelector("#errBox");
      errBox.textContent = "";
      const title = app.querySelector("#titleInput").value.trim() || "Untitled 360";
      startBtn.disabled = true;
      try {
        const { capture } = await OrbitAPI.createCapture(title, mode);
        Router.navigate(`#/capture/${capture.id}`);
      } catch (e) {
        errBox.textContent = "Could not start capture: " + e.message;
        startBtn.disabled = false;
      }
    });

    // --- upload an existing panorama ---
    const panoFile = app.querySelector("#panoFile");
    const panoBtn = app.querySelector("#panoBtn");
    const panoErr = app.querySelector("#panoErr");

    panoBtn.addEventListener("click", () => panoFile.click());

    panoFile.addEventListener("change", async () => {
      const file = panoFile.files && panoFile.files[0];
      if (!file) return;
      panoErr.hidden = true;
      panoBtn.disabled = true;
      panoBtn.textContent = "Uploading…";
      try {
        const title = app.querySelector("#titleInput").value.trim() || file.name.replace(/\.[^.]+$/, "");
        const res = await OrbitAPI.uploadPanorama(title, file);
        Router.navigate(`#/view-id/${res.capture.id}`);
      } catch (e) {
        // The server's refusals explain what the image actually was and what to
        // use instead, so they are shown as written rather than summarised.
        panoErr.textContent = e.message;
        panoErr.hidden = false;
        panoBtn.disabled = false;
        panoBtn.textContent = "Choose a 360 photo";
        panoFile.value = "";
      }
    });

    loadCaptures(app);
    return () => {};
  }

  async function loadCaptures(app) {
    const list = app.querySelector("#captureList");
    try {
      const { captures } = await OrbitAPI.listCaptures(30, 0);
      if (!captures || captures.length === 0) {
        list.innerHTML = `<p class="muted">No captures yet — start your first one above.</p>`;
        return;
      }
      list.innerHTML = captures.map(rowHtml).join("");
      captures.forEach((c) => {
        const row = list.querySelector(`[data-id="${c.id}"]`);
        if (!row) return;
        row.addEventListener("click", () => {
          if (c.status === "ready" || c.status === "partial") {
            Router.navigate(`#/view-id/${c.id}`);
          } else if (c.status === "processing" || c.status === "queued") {
            Router.navigate(`#/processing/${c.id}`);
          } else {
            Router.navigate(`#/capture/${c.id}`);
          }
        });
        bindRowButtons(app, row, c);
      });
    } catch (e) {
      list.innerHTML = `<p class="muted">Could not load captures: ${escapeHtml(e.message)}</p>`;
    }
  }

  // "partial" means it fell back rather than stitched, and "failed" means it
  // did not finish at all. Both usually come down to the run, not the photos,
  // so both are worth another attempt without walking into the capture first.
  function canRebuild(c) {
    return c.status === "partial" || c.status === "failed";
  }

  function rowHtml(c) {
    const badgeClass = c.status === "ready" || c.status === "partial" ? "ready"
      : c.status === "failed" ? "failed"
      : (c.status === "processing" || c.status === "queued") ? "processing" : "";
    const thumb = (c.status === "ready" || c.status === "partial")
      ? `background-image:url('${OrbitAPI.imageURL(c.id, "thumb", 0)}')` : "";
    return `
      <div class="capture-row" data-id="${c.id}" style="cursor:pointer">
        <div class="thumb" style="${thumb}"></div>
        <div class="meta">
          <div class="t">${escapeHtml(c.title)}</div>
          <div class="s">${c.mode === "spin" ? "Object spin" : "Photosphere"} · ${new Date(c.created_at).toLocaleString()}</div>
        </div>
        <span class="badge ${badgeClass}">${c.status}</span>
        ${canRebuild(c) ? `<button class="row-rebuild" title="Build this 360 again from the same photos" aria-label="Build ${escapeHtml(c.title)} again">\u21bb</button>` : ""}
        <button class="row-delete" title="Delete this capture" aria-label="Delete ${escapeHtml(c.title)}">\u00d7</button>
      </div>`;
  }

  function bindRowButtons(app, row, c) {
    const del = row.querySelector(".row-delete");
    if (del) del.addEventListener("click", (ev) => {
      ev.stopPropagation();   // don't open the capture we're deleting
      confirmDelete(app, row, c);
    });
    const rebuild = row.querySelector(".row-rebuild");
    if (rebuild) rebuild.addEventListener("click", async (ev) => {
      ev.stopPropagation();   // don't open the capture we're rebuilding
      rebuild.disabled = true;
      try {
        await OrbitAPI.process(c.id);
        // Watch it from the processing screen, the same as a first build.
        Router.navigate(`#/processing/${c.id}`);
      } catch (e) {
        rebuild.disabled = false;
        row.querySelector(".s").textContent = "Could not start: " + e.message;
      }
    });
  }

  // Deleting is permanent and removes the stored photos too, so the row turns
  // into an inline confirm rather than firing on a single stray tap.
  function confirmDelete(app, row, c) {
    if (row.classList.contains("confirming")) return;
    row.classList.add("confirming");
    const original = row.innerHTML;
    row.innerHTML = `
      <div class="meta">
        <div class="t">Delete &ldquo;${escapeHtml(c.title)}&rdquo;?</div>
        <div class="s">This also deletes its photos. It cannot be undone.</div>
      </div>
      <button class="row-cancel">Cancel</button>
      <button class="row-confirm">Delete</button>`;

    row.querySelector(".row-cancel").addEventListener("click", (ev) => {
      ev.stopPropagation();
      row.classList.remove("confirming");
      row.innerHTML = original;
      // The row's markup was replaced, so every button on it needs binding
      // again - not just the delete one that put it into this state.
      bindRowButtons(app, row, c);
    });

    row.querySelector(".row-confirm").addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const btn = ev.currentTarget;
      btn.disabled = true;
      btn.textContent = "Deleting\u2026";
      try {
        await OrbitAPI.deleteCapture(c.id);
        row.style.height = row.offsetHeight + "px";
        row.classList.add("removing");
        setTimeout(() => loadCaptures(app), 220);
      } catch (e) {
        btn.disabled = false;
        btn.textContent = "Delete";
        row.querySelector(".s").textContent = "Could not delete: " + e.message;
      }
    });
  }

  return { mount };
})();
