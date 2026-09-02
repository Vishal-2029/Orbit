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
                  <div class="title">🌐 Full 360</div>
                  <div class="desc">Dots all around you, plus ceiling and floor. Best result.</div>
                </button>
                <button class="mode-btn" data-mode="pano">
                  <div class="title">📷 Quick 360</div>
                  <div class="desc">One ring around you. Fewer photos, no ceiling or floor.</div>
                </button>
                <button class="mode-btn" data-mode="spin">
                  <div class="title">🔄 Object spin</div>
                  <div class="desc">Turntable — rotate an object in front of a fixed camera.</div>
                </button>
                <button class="mode-btn" data-mode="auto">
                  <div class="title">🖼 Photos I already have</div>
                  <div class="desc">Pick any photos, any order. The stitcher works out how they fit.</div>
                </button>
              </div>
            </div>
            <button id="startBtn" class="primary" style="width:100%">Start capture</button>
            <input id="filePicker" type="file" accept="image/*" multiple hidden />
            <div id="uploadBox" hidden>
              <div id="uploadStatus" class="muted" style="margin-top:10px"></div>
              <div class="upload-bar"><span id="uploadBar"></span></div>
            </div>
            <div id="errBox" class="muted" style="margin-top:8px;color:var(--bad)"></div>
          </div>

          <h3 class="muted" style="margin:22px 0 8px">Previous captures</h3>
          <div id="captureList" class="capture-list"><p class="muted">Loading…</p></div>
        </div>
        <footer class="hint">API: ${window.ORBIT_API_BASE}</footer>
      </div>
    `;

    const startBtn = app.querySelector("#startBtn");
    const filePicker = app.querySelector("#filePicker");
    const uploadBox = app.querySelector("#uploadBox");
    const uploadStatus = app.querySelector("#uploadStatus");
    const uploadBar = app.querySelector("#uploadBar");

    function applyMode() {
      startBtn.textContent = mode === "auto" ? "Choose photos\u2026" : "Start capture";
    }

    app.querySelectorAll(".mode-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        mode = btn.dataset.mode;
        app.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("selected", b === btn));
        applyMode();
      });
    });
    applyMode();

    startBtn.addEventListener("click", async () => {
      const errBox = app.querySelector("#errBox");
      errBox.textContent = "";
      // "Photos I already have" opens the picker instead of the camera.
      if (mode === "auto") {
        filePicker.click();
        return;
      }
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

    filePicker.addEventListener("change", async () => {
      const files = Array.from(filePicker.files || []);
      filePicker.value = "";   // so picking the same files again still fires
      if (!files.length) return;

      const errBox = app.querySelector("#errBox");
      errBox.textContent = "";
      const MIN = 4;
      if (files.length < MIN) {
        errBox.textContent = `Pick at least ${MIN} photos \u2014 ${files.length} is not enough to make a 360.`;
        return;
      }

      const title = app.querySelector("#titleInput").value.trim() || "Untitled 360";
      startBtn.disabled = true;
      uploadBox.hidden = false;

      try {
        const { capture } = await OrbitAPI.createCapture(title, "auto");

        // Sequential, not parallel: phone photos are several MB each, and
        // firing 30 uploads at once on mobile data is how half of them fail.
        let sent = 0;
        const failed = [];
        for (let i = 0; i < files.length; i++) {
          uploadStatus.textContent = `Uploading ${i + 1} of ${files.length}\u2026`;
          uploadBar.style.width = `${Math.round((i / files.length) * 100)}%`;
          try {
            await OrbitAPI.uploadPhoto(capture.id, {
              blob: files[i], index: i, slotId: `photo_${i}`,
              yaw: 0, pitch: 0, hasHeading: false, filename: files[i].name,
            });
            sent++;
          } catch (e) {
            failed.push(files[i].name);
          }
        }
        uploadBar.style.width = "100%";

        if (sent < MIN) {
          throw new Error(
            `Only ${sent} of ${files.length} photos uploaded, which is under the ${MIN} needed.`
          );
        }
        uploadStatus.textContent = failed.length
          ? `${sent} uploaded, ${failed.length} skipped. Building anyway\u2026`
          : "All uploaded. Building your 360\u2026";

        await OrbitAPI.process(capture.id);
        Router.navigate(`#/processing/${capture.id}`);
      } catch (e) {
        uploadBox.hidden = true;
        errBox.textContent = "Could not build a 360 from those photos: " + e.message;
        startBtn.disabled = false;
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
        const del = row.querySelector(".row-delete");
        if (del) del.addEventListener("click", (ev) => {
          ev.stopPropagation();   // don't open the capture we're deleting
          confirmDelete(app, row, c);
        });
      });
    } catch (e) {
      list.innerHTML = `<p class="muted">Could not load captures: ${escapeHtml(e.message)}</p>`;
    }
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
        <button class="row-delete" title="Delete this capture" aria-label="Delete ${escapeHtml(c.title)}">\u00d7</button>
      </div>`;
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
      const del = row.querySelector(".row-delete");
      if (del) del.addEventListener("click", (e2) => {
        e2.stopPropagation();
        confirmDelete(app, row, c);
      });
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
