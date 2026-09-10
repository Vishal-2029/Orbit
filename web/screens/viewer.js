const ScreenViewer = (() => {
  // Editing is offered on the by-id route and not on the public share link.
  //
  // This is a USABILITY boundary, not a security one. There is no
  // authentication in this product yet, so anyone who learns a capture's id can
  // still call the hotspot endpoints directly. What it buys is that a share
  // link never shows edit controls to a visitor, and that a stray click on
  // someone else's tour cannot move their markers. Real ownership checks belong
  // on the server, and go in with auth.
  function canEdit(route) {
    if (window.ORBIT_ALLOW_EDIT === false) return false;
    return !!route.id;
  }

  async function mount(app, { slug, id }) {
    app.innerHTML = `
      <div class="viewer-screen">
        <div id="viewerCanvasHost"></div>
        <div class="viewer-hud">
          <button class="back" onclick="location.hash='#/'" title="Back to home">←</button>
          <div class="title" id="vTitle">Loading…</div>
        </div>
        <div id="sceneList" class="scene-list" hidden></div>
        <div id="loading" class="viewer-loading">
          <div class="spinner"></div>
          <div id="loadingText">Loading manifest…</div>
        </div>
        <div id="degradedBanner"></div>
        <div id="editBar" class="edit-bar" hidden></div>
        <div class="share-box" id="shareBox" style="display:none">
          <input id="shareInput" readonly />
          <button id="copyBtn">Copy</button>
        </div>
      </div>`;

    const screen = app.querySelector(".viewer-screen");
    const host = app.querySelector("#viewerCanvasHost");
    const loading = app.querySelector("#loading");
    const loadingText = app.querySelector("#loadingText");
    const vTitle = app.querySelector("#vTitle");
    const shareBox = app.querySelector("#shareBox");
    const shareInput = app.querySelector("#shareInput");
    const sceneListEl = app.querySelector("#sceneList");
    const editBar = app.querySelector("#editBar");

    let manifest;
    try {
      manifest = slug ? await OrbitAPI.manifestBySlug(slug) : await OrbitAPI.getManifest(id);
    } catch (e) {
      loading.innerHTML = `<div style="text-align:center;padding:20px"><h3>Could not load this 360 view</h3><p class="muted">${escapeHtml(e.message)}</p><button class="primary" onclick="location.hash='#/'">Back home</button></div>`;
      return () => {};
    }

    vTitle.textContent = manifest.title || "360 view";

    // A small "i" button rather than a banner across the view.
    //
    // The message can be four lines long, and as a banner it sat across the top
    // of the panorama covering the thing the user came to look at - on a phone,
    // most of it. It is worth saying, but it is not worth saying over the
    // picture, and nobody needs to read it twice.
    if (manifest.degraded) {
      const why = manifest.degraded_why || "This view uses a fallback renderer.";
      const host = app.querySelector("#degradedBanner");
      host.innerHTML = `
        <button class="degraded-btn" type="button" title="Why does this look like this?"
                aria-label="Why does this look like this?" aria-expanded="false">i</button>
        <div class="degraded-note" hidden>
          <p>${escapeHtml(why)}</p>
          <button class="degraded-close" type="button">Got it</button>
        </div>`;
      const btn = host.querySelector(".degraded-btn");
      const note = host.querySelector(".degraded-note");
      const toggle = (open) => {
        note.hidden = !open;
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      };
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        toggle(note.hidden);
      });
      host.querySelector(".degraded-close").addEventListener("click", (e) => {
        e.stopPropagation();
        toggle(false);
      });
    }

    function setShareLink(forSlug) {
      if (!forSlug) return;
      shareBox.style.display = "flex";
      shareInput.value = `${location.origin}${location.pathname}#/view/${forSlug}`;
    }
    setShareLink(manifest.slug);
    app.querySelector("#copyBtn").addEventListener("click", async () => {
      const btn = app.querySelector("#copyBtn");
      try {
        await navigator.clipboard.writeText(shareInput.value);
        btn.textContent = "Copied!";
        setTimeout(() => (btn.textContent = "Copy"), 1500);
      } catch (_) {
        shareInput.select();
      }
    });

    let instance = null;
    let editing = false;
    const editable = canEdit({ slug, id });

    // ----------------------------------------------------------------------
    // Scenes
    //
    // A capture with no link hotspots has no `scenes` array, so one is made
    // from the manifest's own fields. That keeps the two cases identical from
    // here on rather than branching through the whole file.
    // ----------------------------------------------------------------------
    function scenesFromManifest(m) {
      if (m.scenes && m.scenes.length) {
        return m.scenes.map((s) => ({
          id: s.id,
          slug: s.slug,
          title: s.title,
          panorama: resolveUrl(s.panorama),
          width: s.width,
          tiles: s.tiles ? resolveUrl(s.tiles) : "",
          preview: s.preview ? resolveUrl(s.preview) : "",
          levels: s.levels,
          hotspots: s.hotspots || [],
        }));
      }
      return [{
        id: m.capture_id,
        slug: m.slug,
        title: m.title,
        panorama: resolveUrl(m.panorama),
        width: m.width,
        tiles: m.tiles ? resolveUrl(m.tiles) : "",
        preview: m.preview ? resolveUrl(m.preview) : "",
        levels: m.levels,
        hotspots: m.hotspots || [],
      }];
    }

    let scenes = scenesFromManifest(manifest);

    // Opening a share link for a scene deep inside a tour should land on THAT
    // scene, not on whichever one the manifest happens to list first.
    const startSceneId =
      (slug && (scenes.find((s) => s.slug === slug) || {}).id) || scenes[0].id;

    if (manifest.renderer === "sphere") {
      instance = SphereViewer.create(host, scenes, {
        startSceneId,
        editable: editing,
        onProgress: (progress, err) => {
          if (err) {
            loadingText.textContent = "Failed to load the panorama.";
            return;
          }
          if (progress >= 1) loading.style.display = "none";
        },
        onSceneChange: onSceneChange,
        onPlace: onPlace,
        onEdit: onEditHotspot,
        onDelete: onDeleteHotspot,
      });
      if (instance) {
        buildSphereControls(screen, instance);
        buildSceneList();
        if (editable) buildEditBar();
      }
    } else if (manifest.renderer === "frames") {
      const urls = (manifest.frames || []).map(resolveUrl);
      if (urls.length === 0) {
        loadingText.textContent = "This capture has no frames to show.";
      } else {
        instance = FramesViewer.create(host, urls, (progress) => {
          if (progress >= 1) loading.style.display = "none";
          else loadingText.textContent = `Loading frames… ${Math.round(progress * 100)}%`;
        }, { yaws: manifest.yaws, pitches: manifest.pitches });
      }
    } else {
      loadingText.textContent = `Unknown renderer "${manifest.renderer}".`;
    }

    // ----------------------------------------------------------------------
    // Scene switching
    // ----------------------------------------------------------------------
    function onSceneChange(scene) {
      vTitle.textContent = scene.title || "360 view";
      setShareLink(scene.slug);
      sceneListEl.querySelectorAll(".scene-item").forEach((el) => {
        el.classList.toggle("active", el.dataset.sceneId === scene.id);
      });
      // replaceState rather than assigning location.hash: assigning it fires
      // hashchange, the router re-renders, and the viewer is destroyed and
      // rebuilt - which throws away the cross-fade that made walking through a
      // door feel like walking through a door.
      if (scene.slug && !editable) {
        try {
          history.replaceState(null, "", `${location.pathname}#/view/${scene.slug}`);
        } catch (_) {}
      }
    }

    function buildSceneList() {
      if (scenes.length < 2) return;
      sceneListEl.hidden = false;
      sceneListEl.innerHTML = "";
      const heading = document.createElement("div");
      heading.className = "scene-list-title";
      heading.textContent = `${scenes.length} places`;
      sceneListEl.appendChild(heading);
      scenes.forEach((s) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "scene-item" + (s.id === startSceneId ? " active" : "");
        item.dataset.sceneId = s.id;
        item.textContent = s.title || "Untitled 360";
        item.addEventListener("click", () => instance.switchTo(s.id));
        sceneListEl.appendChild(item);
      });
    }

    // ----------------------------------------------------------------------
    // Editing
    // ----------------------------------------------------------------------
    let myCaptures = [];

    async function loadCaptureChoices() {
      if (myCaptures.length) return myCaptures;
      try {
        const res = await OrbitAPI.listCaptures(100, 0);
        // Only a finished capture can be linked to: an arrow leading to one
        // still processing would open a black screen, and the server refuses it
        // anyway. Filtering here turns that into an option that is simply not
        // offered.
        myCaptures = (res.captures || []).filter(
          (c) => c.status === "ready" || c.status === "partial");
      } catch (_) {
        myCaptures = [];
      }
      return myCaptures;
    }

    function buildEditBar() {
      editBar.hidden = false;
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "edit-toggle";
      const label = () => (editing ? "Done" : "Add hotspots");
      toggle.textContent = label();
      toggle.addEventListener("click", async () => {
        editing = !editing;
        toggle.textContent = label();
        toggle.classList.toggle("active", editing);
        screen.classList.toggle("placing", editing);
        hint.hidden = !editing;
        if (editing) await loadCaptureChoices();
        // The viewer builds hotspot elements once, with or without their edit
        // controls, so switching mode means rebuilding them.
        rebuild();
      });
      const hint = document.createElement("div");
      hint.className = "edit-hint";
      hint.hidden = true;
      hint.textContent = "Click anywhere in the 360 to place a hotspot";
      editBar.appendChild(toggle);
      editBar.appendChild(hint);
    }

    // Rebuilding the whole viewer on every edit would reload the panorama and
    // throw away where the user was looking. Re-attaching just this scene's
    // hotspots keeps both.
    async function refreshHotspots(captureId) {
      const res = await OrbitAPI.listHotspots(captureId);
      const list = res.hotspots || [];
      const scene = scenes.find((s) => s.id === captureId);
      if (scene) scene.hotspots = list;
      instance.reloadHotspots(captureId, list);
    }

    function rebuild() {
      const sceneId = instance.currentSceneId();
      instance.setEditable(editing);
      instance.reloadHotspots(sceneId,
        (scenes.find((s) => s.id === sceneId) || {}).hotspots || []);
    }

    async function onPlace({ sceneId, yaw, pitch }) {
      if (!editing) return;
      HotspotEditor.open({
        hotspot: { kind: "info", yaw, pitch },
        captures: await loadCaptureChoices(),
        currentCaptureId: sceneId,
        onSave: async (payload) => {
          await OrbitAPI.createHotspot(sceneId, payload);
          await refreshHotspots(sceneId);
        },
      });
    }

    async function onEditHotspot(hotspot) {
      if (!editing) return;
      HotspotEditor.open({
        hotspot,
        captures: await loadCaptureChoices(),
        currentCaptureId: hotspot.capture_id,
        onSave: async (payload) => {
          await OrbitAPI.updateHotspot(hotspot.id, payload);
          await refreshHotspots(hotspot.capture_id);
        },
        onDelete: async (h) => {
          await OrbitAPI.deleteHotspot(h.id);
          await refreshHotspots(h.capture_id);
        },
      });
    }

    async function onDeleteHotspot(hotspot) {
      if (!editing) return;
      await OrbitAPI.deleteHotspot(hotspot.id);
      await refreshHotspots(hotspot.capture_id);
    }

    // ----------------------------------------------------------------------
    // Wheel, pinch and keyboard already do all of this, but none of them are
    // discoverable and none exist on a phone. The bar is the only way most
    // people will find zoom, fullscreen or the gyroscope.
    // ----------------------------------------------------------------------
    function buildSphereControls(parent, viewer) {
      const bar = document.createElement("div");
      bar.className = "viewer-tools";
      const mk = (label, title, onClick, cls) => {
        const b = document.createElement("button");
        b.className = "viewer-tool" + (cls ? " " + cls : "");
        b.type = "button";
        b.textContent = label;
        b.title = title;
        b.setAttribute("aria-label", title);
        b.addEventListener("click", (e) => { e.stopPropagation(); onClick(b); });
        bar.appendChild(b);
        return b;
      };

      // Download the flat equirectangular image. It is the file every other 360
      // tool wants - Google Maps, Facebook, Pannellum, a VR headset - so being
      // able to take it out of here matters more than it looks.
      //
      // A plain <a download> would be simpler, but it only works same-origin;
      // the panorama is served from the API host, so the attribute is ignored
      // and the browser navigates to the image instead of saving it. Fetching
      // it as a blob works whatever the origin, and ?download=1 makes the
      // server name the file after the capture.
      if (manifest.renderer === "sphere" && manifest.panorama) {
        mk("⭳", "Download this 360 as an image", async (b) => {
          const was = b.textContent;
          b.textContent = "…";
          b.disabled = true;
          try {
            const url = resolveUrl(manifest.panorama) +
              (manifest.panorama.indexOf("?") === -1 ? "?" : "&") + "download=1";
            const res = await fetch(url);
            if (!res.ok) throw new Error("HTTP " + res.status);
            // Make the saved file a valid 360 rather than a wide photo: an
            // exact 2:1 canvas with GPano XMP, so Photos, Facebook and a
            // headset open it as a sphere. Older panoramas are a pixel off
            // 2:1, which is enough for a strict viewer to refuse them.
            const blob = await GPano.prepareDownload(await res.blob());
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = (manifest.title || "360").replace(/[\\/:*?"<>|]/g, "-") + ".jpg";
            document.body.appendChild(a);
            a.click();
            a.remove();
            // Revoked on a delay: revoking immediately can cancel the save in
            // some browsers before it has read the blob.
            setTimeout(() => URL.revokeObjectURL(a.href), 20000);
            b.textContent = "✓";
            setTimeout(() => { b.textContent = was; b.disabled = false; }, 1600);
          } catch (e) {
            b.textContent = "✕";
            b.title = "Could not download: " + e.message;
            setTimeout(() => { b.textContent = was; b.disabled = false; }, 2200);
          }
        });
      }

      mk("−", "Zoom out", () => viewer.zoomOut());
      mk("+", "Zoom in", () => viewer.zoomIn());
      mk("↺", "Reset the view", () => viewer.resetView());

      const spin = mk("◌", "Pause the slow drift", (b) => {
        const on = viewer.setAutoRotate(!viewer.isAutoRotating());
        b.classList.toggle("off", !on);
        b.title = on ? "Pause the slow drift" : "Let the view drift again";
      });
      spin.classList.toggle("off", !viewer.isAutoRotating());

      // Device orientation needs a user gesture on iOS, so it stays a button
      // and disappears once granted - there is no turning it back off.
      if (window.DeviceOrientationEvent) {
        mk("\u{1F9ED}", "Look around by moving the phone", (b) => {
          if (viewer.enableOrientation()) b.remove();
        });
      }
      if (document.fullscreenEnabled) {
        mk("⛶", "Fullscreen", () => viewer.toggleFullscreen());
      }
      parent.appendChild(bar);
    }

    function resolveUrl(u) {
      if (!u) return u;
      if (/^https?:\/\//.test(u)) return u;
      // Manifest URLs may be relative to the API host.
      return u.startsWith("/") ? window.ORBIT_API_BASE + u : u;
    }

    return () => {
      HotspotEditor.close();
      if (instance) instance.destroy();
    };
  }

  return { mount };
})();
