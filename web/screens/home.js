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

          <h3 class="muted" style="margin:22px 0 8px">Previous captures</h3>
          <div id="captureList" class="capture-list"><p class="muted">Loading…</p></div>
        </div>
        <footer class="hint">API: ${window.ORBIT_API_BASE}</footer>
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
