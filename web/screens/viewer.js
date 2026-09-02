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
      // Offer device-orientation look-around behind a tap, iOS-safe.
      const btn = document.createElement("button");
      btn.textContent = "🧭 Look around with device";
      btn.style.cssText = "position:absolute;top:64px;right:16px;z-index:3;background:rgba(0,0,0,.5);color:#fff;";
      btn.addEventListener("click", () => {
        const ok = instance.enableOrientation();
        btn.remove();
      });
      host.parentElement.appendChild(btn);
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
        });
      }
    } else {
      loadingText.textContent = `Unknown renderer "${manifest.renderer}".`;
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
