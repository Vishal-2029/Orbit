const ScreenViewer = (() => {
  async function mount(app, { slug, id }) {
    app.innerHTML = `
      <div class="viewer-screen">
        <div id="viewerCanvasHost"></div>
        <div class="viewer-hud">
          <button class="back" onclick="location.hash='#/'" title="Back to home">←</button>
          <div class="title" id="vTitle">Loading…</div>
        </div>
        <div id="loading" class="viewer-loading">
          <div class="spinner"></div>
          <div id="loadingText">Loading manifest…</div>
        </div>
        <div id="degradedBanner"></div>
        <div class="share-box" id="shareBox" style="display:none">
          <input id="shareInput" readonly />
          <button id="copyBtn">Copy</button>
        </div>
      </div>`;

    const host = app.querySelector("#viewerCanvasHost");
    const loading = app.querySelector("#loading");
    const loadingText = app.querySelector("#loadingText");
    const vTitle = app.querySelector("#vTitle");
    const shareBox = app.querySelector("#shareBox");
    const shareInput = app.querySelector("#shareInput");

    let manifest;
    try {
      manifest = slug ? await OrbitAPI.manifestBySlug(slug) : await OrbitAPI.getManifest(id);
    } catch (e) {
      loading.innerHTML = `<div style="text-align:center;padding:20px"><h3>Could not load this 360 view</h3><p class="muted">${escapeHtml(e.message)}</p><button class="primary" onclick="location.hash='#/'">Back home</button></div>`;
      return () => {};
    }

    vTitle.textContent = manifest.title || "360 view";

    if (manifest.degraded) {
      app.querySelector("#degradedBanner").innerHTML =
        `<div class="banner warn" style="position:absolute;top:64px;left:16px;right:16px;z-index:3">${escapeHtml(manifest.degraded_why || "This view uses a fallback renderer.")}</div>`;
    }

    if (manifest.slug) {
      shareBox.style.display = "flex";
      const shareUrl = `${location.origin}${location.pathname}#/view/${manifest.slug}`;
      shareInput.value = shareUrl;
      app.querySelector("#copyBtn").addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(shareUrl);
          app.querySelector("#copyBtn").textContent = "Copied!";
          setTimeout(() => (app.querySelector("#copyBtn").textContent = "Copy"), 1500);
        } catch (_) {
          shareInput.select();
        }
      });
    }

    let instance = null;

    if (manifest.renderer === "sphere") {
      const panoUrl = resolveUrl(manifest.panorama);
      instance = SphereViewer.create(host, panoUrl, (progress, err) => {
        if (err) {
          loadingText.textContent = "Failed to load panorama image.";
          return;
        }
        if (progress >= 1) {
          loading.style.display = "none";
        } else {
          loadingText.textContent = `Loading panorama… ${Math.round(progress * 100)}%`;
        }
      });
      buildSphereControls(host.parentElement, instance);
    } else if (manifest.renderer === "frames") {
      const urls = (manifest.frames || []).map(resolveUrl);
      if (urls.length === 0) {
        loadingText.textContent = "This capture has no frames to show.";
      } else {
        instance = FramesViewer.create(host, urls, (progress) => {
          if (progress >= 1) {
            loading.style.display = "none";
          } else {
            loadingText.textContent = `Loading frames… ${Math.round(progress * 100)}%`;
          }
        }, { yaws: manifest.yaws, pitches: manifest.pitches });
      }
    } else {
      loadingText.textContent = `Unknown renderer "${manifest.renderer}".`;
    }

    // Wheel, pinch and keyboard already do all of this, but none of them are
    // discoverable and none exist on a phone. The bar is the only way most
    // people will find zoom, fullscreen or the gyroscope.
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

      mk("\u2212", "Zoom out", () => viewer.zoomOut());
      mk("+", "Zoom in", () => viewer.zoomIn());
      mk("\u21ba", "Reset the view", () => viewer.resetView());

      const spin = mk("\u25cc", "Pause the slow drift", (b) => {
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
        mk("\u26f6", "Fullscreen", () => viewer.toggleFullscreen());
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
      if (instance) instance.destroy();
    };
  }

  return { mount };
})();
