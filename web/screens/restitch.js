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
  // Names and directions come from PhotoNames, shared with the 360 viewer's
  // photo labels, so a photo is called the same thing on both screens.
  const position = PhotoNames.position;

  // Deciding whether a photo is blurred, or of the wrong room, cannot be done
  // from a 56px square. Tapping one opens the full original - the actual upload,
  // not the downscaled copy the stitcher works from.
  let lightbox = null;
  let gallery = [];      // [{ src, label }] in shot order
  let galleryAt = 0;

  function closeLightbox() {
    if (!lightbox) return;
    document.removeEventListener("keydown", onLightboxKey);
    lightbox.remove();
    lightbox = null;
  }

  function onLightboxKey(e) {
    if (e.key === "Escape") closeLightbox();
    else if (e.key === "ArrowRight") showPhoto(galleryAt + 1);
    else if (e.key === "ArrowLeft") showPhoto(galleryAt - 1);
  }

  // Stops at the ends rather than wrapping: the photos are in shot order, and
  // jumping from the last straight back to the first would hide that you had
  // reached the end.
  function showPhoto(i) {
    if (!lightbox || i < 0 || i >= gallery.length) return;
    galleryAt = i;
    const g = gallery[i];
    const img = lightbox.querySelector("img");
    img.src = g.src;
    img.alt = g.label;
    lightbox.querySelector(".lightbox-label").textContent =
      `${g.label}   (${i + 1} / ${gallery.length})`;
    lightbox.querySelector(".lightbox-prev").disabled = i === 0;
    lightbox.querySelector(".lightbox-next").disabled = i === gallery.length - 1;
    // Warm the neighbours, so the next swipe shows a photo rather than a gap.
    [i - 1, i + 1].forEach((j) => {
      if (j >= 0 && j < gallery.length) new Image().src = gallery[j].src;
    });
  }

  // Opens on one photo and pages through them all - reviewing a capture means
  // looking at every photo, and closing and reopening for each was the
  // slowest part of the screen.
  function openLightbox(photos, start) {
    closeLightbox();
    gallery = photos;
    lightbox = document.createElement("div");
    lightbox.className = "lightbox";
    lightbox.innerHTML = `
      <button class="lightbox-close" aria-label="Close photo">×</button>
      <button class="lightbox-nav lightbox-prev" aria-label="Previous photo">‹</button>
      <img alt="">
      <button class="lightbox-nav lightbox-next" aria-label="Next photo">›</button>
      <div class="lightbox-label"></div>`;

    // Only the backdrop and the close button dismiss it now. With swiping, a
    // tap on the photo itself is far too often the end of a swipe.
    lightbox.addEventListener("click", (e) => {
      if (e.target === lightbox || e.target.closest(".lightbox-close")) closeLightbox();
    });
    lightbox.querySelector(".lightbox-prev").addEventListener("click", (e) => {
      e.stopPropagation(); showPhoto(galleryAt - 1);
    });
    lightbox.querySelector(".lightbox-next").addEventListener("click", (e) => {
      e.stopPropagation(); showPhoto(galleryAt + 1);
    });

    // Swipe: a mostly-horizontal drag of 40px or more turns the page.
    let x0 = null, y0 = null;
    lightbox.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) { x0 = null; return; }
      x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
    }, { passive: true });
    lightbox.addEventListener("touchend", (e) => {
      if (x0 === null) return;
      const t = e.changedTouches[0];
      const dx = t.clientX - x0, dy = t.clientY - y0;
      x0 = null;
      if (Math.abs(dx) >= 40 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        showPhoto(galleryAt + (dx < 0 ? 1 : -1));
      }
    }, { passive: true });

    document.addEventListener("keydown", onLightboxKey);
    document.body.appendChild(lightbox);
    showPhoto(start);
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
    const ringRows = {};    // ring id -> how that ring stitched on its own
    try {
      const [cap, fr, rg] = await Promise.all([
        OrbitAPI.getCapture(captureId),
        OrbitAPI.listFrames(captureId),
        // A ring preview is a bonus - an older capture has none, and the
        // screen has to work the same without them.
        OrbitAPI.listRings(captureId).catch(() => ({ rings: [] })),
      ]);
      capture = cap.capture;
      frames = fr.frames || [];
      (rg.rings || []).forEach((r) => { ringRows[r.ring] = r; });
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
            <button id="arrangeBtn" style="width:100%;margin-bottom:10px">Place the photos by hand</button>
            <div class="muted" style="margin:-4px 0 12px;font-size:.85rem">
              For a join the stitcher cannot work out on its own — a blank wall,
              a repeating window. Every photo, whole, where it would be placed.
            </div>
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
      // Grouped the way the capture was shot - level ring, then tilted up, then
      // tilted down - because that is how a capture is judged: a whole ring is
      // usually good or bad together, and "the top ring is soft" is a decision
      // you can act on, where thirty numbered photos is not.
      const num = PhotoNames.numbers(frames);
      rows.innerHTML = PhotoNames.byRing(frames).map((g) => {
        const used = g.frames.filter((f) => !f.excluded).length;
        const row = ringRows[g.ring.id];
        // The ring's own stitch, made while the capture was still being shot.
        // Worth showing here because it answers the question this screen asks -
        // is this ring any good? - without rebuilding the whole 360 first.
        // Where the camera travelled rather than turned. Worth saying next to
        // the ring, because it names the photos whose joins will tear and that
        // only a reshoot - turning on the spot - can fix.
        const moved = row && row.moved && row.moved.length
          ? `<div class="ring-moved">The camera moved at ${escapeHtml(PhotoNames.movedText(row.moved, num))}.
               Reshoot those turning the phone on the spot to fix the tears there.</div>`
          : "";
        const preview = row && row.panorama ? `
          <div class="ring-preview">
            <img src="${row.panorama}" alt="${escapeHtml(g.ring.label)} stitched"
                 loading="lazy" onerror="this.closest('.ring-preview').hidden=true">
            <div class="muted">${escapeHtml(row.note || "")}</div>
            ${moved}
          </div>` : moved;
        const state = row
          ? `<span class="badge ${row.status === "ready" ? "ready" : row.status === "failed" ? "failed" : "processing"}">${escapeHtml(row.status)}</span>`
          : "";
        // A capture shot before rings were stitched separately has none, and a
        // ring can always be built again after a photo is removed from it.
        // Either way the button says the same thing: build this ring alone.
        const busy = row && (row.status === "queued" || row.status === "processing");
        const ringBtn = used >= 2
          ? `<button class="ring-build" data-ring="${escapeHtml(g.ring.id)}" ${busy ? "disabled" : ""}>${busy ? "Stitching\u2026" : row ? "Stitch again" : "Stitch this ring"}</button>`
          : "";
        return `
          <div class="ring-head">
            <span class="ring-name">${escapeHtml(g.ring.label)}</span>
            <span class="ring-hint">${escapeHtml(g.ring.hint)}</span>
            ${state}
            <span class="ring-count">${used} of ${g.frames.length}</span>
            ${ringBtn}
          </div>
          ${preview}
          ${g.frames.map((f) => rowHtml(f, num[f.index], ver)).join("")}`;
      }).join("");
      // Every photo left out means nothing to build from, and the server says
      // so too - but saying it here keeps the button honest rather than
      // offering a click that can only fail.
      goBtn.disabled = used === 0;
      if (used === 0) {
        goNote.textContent = "Put at least one photo back to build.";
      }

      rows.querySelectorAll(".ring-build").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const id = btn.dataset.ring;
          btn.disabled = true;
          btn.textContent = "Stitching\u2026";
          try {
            await OrbitAPI.processRing(captureId, id);
            ringRows[id] = Object.assign({}, ringRows[id] || {}, { ring: id, status: "queued" });
            watchRings();
          } catch (e) {
            btn.disabled = false;
            btn.textContent = "Could not start";
            btn.title = e.message;
          }
        });
      });

      const openers = Array.from(rows.querySelectorAll(".photo-open"));
      openers.forEach((img, i) => {
        img.addEventListener("click", () => {
          openLightbox(openers.map((o) => ({ src: o.dataset.full, label: o.dataset.label })), i);
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

    // While a ring is stitching, keep asking until it lands. Only runs while
    // one is actually in flight, and stops itself when none is.
    let ringTimer = null;
    function watchRings() {
      if (ringTimer) return;
      ringTimer = setInterval(async () => {
        let rings = [];
        try {
          rings = (await OrbitAPI.listRings(captureId)).rings || [];
        } catch (_) {
          return;
        }
        rings.forEach((r) => { ringRows[r.ring] = r; });
        draw();
        if (!rings.some((r) => r.status === "queued" || r.status === "processing")) {
          clearInterval(ringTimer);
          ringTimer = null;
        }
      }, 4000);
    }
    if (Object.values(ringRows).some((r) => r.status === "queued" || r.status === "processing")) {
      watchRings();
    }
          } catch (e) {
            btn.disabled = false;
            btn.textContent = "Failed";
            btn.title = e.message;
          }
        });
      });
    }

    draw();

    app.querySelector("#arrangeBtn").addEventListener("click", () => {
      Router.navigate(`#/arrange/${captureId}`);
    });

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
    return () => {
      if (ringTimer) clearInterval(ringTimer);
      closeLightbox();
    };
  }

  return { mount };
})();
