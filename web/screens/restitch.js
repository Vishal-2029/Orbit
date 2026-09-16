// Review the photos of a finished capture, drop the ones spoiling it, build again.
//
// A rebuild from the identical set of photos returns the identical 360, so the
// button that starts one is only useful next to the decision it depends on:
// which photos to use. Hence one screen for both - the photos in the order they
// were shot, each with where the camera was pointing, and a build button under
// them.
//
// Removing here is EXCLUDING, never deleting. Which photo is hurting a stitch
// is a guess until you rebuild and look, so it has to be a guess you can take
// back; the photo and its files stay on the server either way.
const ScreenRestitch = (() => {
  // Where the camera was pointing, in words rather than numbers alone. The
  // capture flow talks to people in compass terms ("turn a quarter turn to your
  // RIGHT"), so the review of it should read the same way.
  function facing(yaw) {
    const y = ((Number(yaw) || 0) % 360 + 360) % 360;
    const names = ["ahead", "ahead-right", "right", "behind-right",
                   "behind", "behind-left", "left", "ahead-left"];
    return names[Math.round(y / 45) % 8];
  }

  function tilt(pitch) {
    const p = Math.round(Number(pitch) || 0);
    if (p >= 30) return ", tilted up";
    if (p <= -30) return ", tilted down";
    return "";
  }

  function position(f) {
    const y = Math.round(Number(f.yaw) || 0);
    const p = Math.round(Number(f.pitch) || 0);
    return `${facing(f.yaw)}${tilt(f.pitch)} · ${y}°, ${p >= 0 ? "+" : ""}${p}°`;
  }

  // Deciding whether a photo is blurred, or of the wrong room, cannot be done
  // from a 56px square. Tapping one opens the full original - the actual upload,
  // not the downscaled copy the stitcher works from.
  let lightbox = null;

  function closeLightbox() {
    if (!lightbox) return;
    document.removeEventListener("keydown", onLightboxKey);
    lightbox.remove();
    lightbox = null;
  }

  function onLightboxKey(e) {
    if (e.key === "Escape") closeLightbox();
  }

  function openLightbox(src, label) {
    closeLightbox();
    lightbox = document.createElement("div");
    lightbox.className = "lightbox";
    lightbox.innerHTML = `
      <button class="lightbox-close" aria-label="Close photo">×</button>
      <img src="${src}" alt="${escapeHtml(label)}">
      <div class="lightbox-label">${escapeHtml(label)}</div>`;
    // Anywhere closes it, the image included: this is a look, not a workspace,
    // and hunting for a close button on a phone is worse than the odd
    // accidental dismissal.
    lightbox.addEventListener("click", closeLightbox);
    document.addEventListener("keydown", onLightboxKey);
    document.body.appendChild(lightbox);
  }

  function rowHtml(f, n, ver) {
    // The thumbnail is served from the index, so it is there whenever the
    // capture has been built once. onerror falls back to the original upload
    // for a capture that never finished a build and has no thumbnail yet.
    //
    // ver stamps the thumbnail because a rebuild rewrites it in place; the
    // original never changes once uploaded, so it does not need one.
    const thumb = OrbitAPI.imageURL(f.capture_id, "thumb", f.index, ver);
    const full = OrbitAPI.imageURL(f.capture_id, "original", f.index);
    const off = f.excluded;
    return `
      <div class="capture-row" data-idx="${f.index}" style="${off ? "opacity:.45" : ""}">
        <img class="thumb photo-open" src="${thumb}" alt="Photo ${n}"
             data-full="${full}" data-label="Photo ${n} — ${escapeHtml(position(f))}"
             title="Tap to see this photo full size"
             onerror="this.onerror=null;this.src='${full}'"
             style="object-fit:cover;width:56px;height:56px;border-radius:8px;flex:0 0 auto;cursor:zoom-in">
        <div class="meta">
          <div class="t">Photo ${n}${off ? " — not used" : ""}</div>
          <div class="s">${escapeHtml(position(f))}</div>
        </div>
        ${f.status === "failed" ? `<span class="badge failed">failed</span>` : ""}
        <button class="row-toggle" title="${off ? "Use this photo again" : "Leave this photo out of the 360"}">
          ${off ? "Put back" : "Remove"}
        </button>
      </div>`;
  }

  async function mount(app, captureId) {
    let capture, frames;
    try {
      const [cap, fr] = await Promise.all([
        OrbitAPI.getCapture(captureId),
        OrbitAPI.listFrames(captureId),
      ]);
      capture = cap.capture;
      frames = fr.frames || [];
    } catch (e) {
      app.innerHTML = `<div class="container"><div class="card"><h2>Not found</h2>
        <p class="muted">${escapeHtml(e.message)}</p>
        <button class="primary" onclick="location.hash='#/'">Back home</button></div></div>`;
      return () => {};
    }

    app.innerHTML = `
      <div class="screen">
        <div class="topbar">
          <button class="back" onclick="location.hash='#/'" title="Back to home">←</button>
          <h1>Build this 360 again</h1>
        </div>
        <div class="container">
          <div class="card">
            <div class="muted" id="countLine"></div>
            <div class="muted" style="margin-top:6px;font-size:.85rem">
              Photos are listed in the order you shot them. Tap one to see it
              full size. Remove one that is blurred, or of somewhere else, and
              build again — nothing is deleted, so you can put it back and try
              the other way round.
            </div>
          </div>
          <div id="rows"></div>
          <div class="card">
            <button class="primary" id="goBtn" style="width:100%">Build it again</button>
            <div class="muted" id="goNote" style="margin-top:8px;font-size:.85rem">
              Uses the photos above. The previous result is replaced.
            </div>
          </div>
        </div>
      </div>`;

    const rows = app.querySelector("#rows");
    const countLine = app.querySelector("#countLine");
    const goBtn = app.querySelector("#goBtn");
    const goNote = app.querySelector("#goNote");

    function draw() {
      const used = frames.filter((f) => !f.excluded).length;
      countLine.textContent =
        `${capture.title} — ${used} of ${frames.length} photos will be used`;
      const ver = OrbitAPI.cacheStamp(capture);
      rows.innerHTML = frames.map((f, i) => rowHtml(f, i + 1, ver)).join("");
      // Every photo left out means nothing to build from, and the server says
      // so too - but saying it here keeps the button honest rather than
      // offering a click that can only fail.
      goBtn.disabled = used === 0;
      if (used === 0) {
        goNote.textContent = "Put at least one photo back to build.";
      }

      rows.querySelectorAll(".photo-open").forEach((img) => {
        img.addEventListener("click", () => {
          openLightbox(img.dataset.full, img.dataset.label);
        });
      });

      rows.querySelectorAll(".row-toggle").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const idx = Number(btn.closest(".capture-row").dataset.idx);
          const f = frames.find((x) => x.index === idx);
          if (!f) return;
          btn.disabled = true;
          try {
            // Redraw from the server's answer, not from the click: if the
            // write did not land, the screen must not claim it did.
            const res = await OrbitAPI.setFrameExcluded(captureId, idx, !f.excluded);
            frames = res.frames || frames;
            draw();
          } catch (e) {
            btn.disabled = false;
            btn.textContent = "Failed";
            btn.title = e.message;
          }
        });
      });
    }

    draw();

    goBtn.addEventListener("click", async () => {
      goBtn.disabled = true;
      goBtn.textContent = "Starting…";
      try {
        await OrbitAPI.process(captureId);
        Router.navigate(`#/processing/${captureId}`);
      } catch (e) {
        goBtn.disabled = false;
        goBtn.textContent = "Build it again";
        goNote.textContent = "Could not start: " + e.message;
      }
    });

    // Leaving the screen with a photo open must not leave the overlay - or its
    // key listener - behind on whatever comes next.
    return () => closeLightbox();
  }

  return { mount };
})();
