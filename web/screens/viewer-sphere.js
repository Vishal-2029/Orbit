// Equirectangular panorama viewer on an inverted three.js sphere.
//
// Drag to look, wheel or pinch to zoom, double-click to zoom in, arrow keys to
// pan, F for fullscreen. Movement carries momentum and settles, and the view
// drifts on its own once you stop touching it, because a panorama that sits
// perfectly still reads as a flat photo rather than somewhere you are standing.
const SphereViewer = (() => {
  // Look angles are held in degrees and smoothed toward a target every frame,
  // rather than being written straight from the pointer. That one indirection
  // is what makes dragging feel weighted instead of glued to the cursor.
  const DAMPING = 0.12;          // 0 = never arrives, 1 = no smoothing at all
  const DRAG_SPEED = 0.13;       // degrees per pixel dragged
  const FLICK_DECAY = 0.94;      // how quickly a flick runs out
  const MIN_FOV = 25, MAX_FOV = 100, START_FOV = 75;
  const IDLE_BEFORE_DRIFT_MS = 4000;
  const DRIFT_DEG_PER_SEC = 1.8;
  const MAX_PIXEL_RATIO = 2;     // 3x on a phone costs a lot and shows nothing

  function create(host, panoramaUrl, onLoadProgress, opts) {
    const options = opts || {};
    const width = () => host.clientWidth || 1;
    const height = () => host.clientHeight || 1;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(START_FOV, width() / height(), 0.1, 1100);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    // A phone reporting devicePixelRatio 3 renders nine times the pixels of a
    // CSS-pixel buffer for no visible gain on a photographic texture.
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO));
    renderer.setSize(width(), height());
    // Without this the panorama renders noticeably flat and washed out: the
    // JPEG is sRGB, and three.js assumes linear unless told otherwise.
    if (THREE.sRGBEncoding !== undefined) renderer.outputEncoding = THREE.sRGBEncoding;
    host.appendChild(renderer.domElement);

    // More segments than the usual 60x40. The seam down the back of a sphere is
    // where straight lines bend most, and the extra geometry is free next to
    // the cost of the texture.
    const geometry = new THREE.SphereGeometry(500, 96, 64);
    geometry.scale(-1, 1, 1); // invert so the texture renders on the inside

    let mesh = null;
    let texture = null;

    // A GPU refuses a texture wider than MAX_TEXTURE_SIZE and the panorama
    // simply never appears - black sphere, no error. Older phones report 4096,
    // which a finished panorama can exceed, so downscale rather than fail.
    function fitToGPU(image) {
      const max = renderer.capabilities.maxTextureSize || 4096;
      if (image.width <= max && image.height <= max) return image;
      const scale = max / Math.max(image.width, image.height);
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.floor(image.width * scale));
      canvas.height = Math.max(1, Math.floor(image.height * scale));
      canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
      return canvas;
    }

    const loader = new THREE.TextureLoader();
    loader.setCrossOrigin("anonymous");
    loader.load(
      panoramaUrl,
      (tex) => {
        tex.image = fitToGPU(tex.image);
        if (THREE.sRGBEncoding !== undefined) tex.encoding = THREE.sRGBEncoding;
        // Anisotropy is what keeps the ceiling and floor legible: they are the
        // parts of an equirectangular image viewed at the sharpest angle, and
        // without it they smear as soon as you look up.
        tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
        tex.minFilter = THREE.LinearMipmapLinearFilter;
        tex.generateMipmaps = true;
        tex.needsUpdate = true;
        texture = tex;
        mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ map: tex }));
        scene.add(mesh);
        onLoadProgress && onLoadProgress(1);
      },
      (xhr) => { if (xhr.total) onLoadProgress && onLoadProgress(xhr.loaded / xhr.total); },
      (err) => { onLoadProgress && onLoadProgress(-1, err); }
    );

    // Where the camera is looking, and where it is heading.
    let lon = 0, lat = 0;
    let targetLon = 0, targetLat = 0;
    let fov = START_FOV, targetFov = START_FOV;
    let spinVelocity = 0;                  // degrees per frame, left over from a flick
    let dragging = false, lastX = 0, lastY = 0, moved = false;
    let lastInteraction = performance.now();
    let autoRotate = options.autoRotate !== false;

    function touched() {
      lastInteraction = performance.now();
    }

    function setTargetLat(v) {
      // Stop just short of the poles: an equirectangular projection has no
      // detail there, and looking straight up flips the horizon over.
      targetLat = Math.max(-85, Math.min(85, v));
    }

    function onPointerDown(x, y) {
      dragging = true; moved = false; lastX = x; lastY = y;
      spinVelocity = 0;
      touched();
    }
    function onPointerMove(x, y) {
      if (!dragging) return;
      const dx = x - lastX, dy = y - lastY;
      if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
      // Zoomed in, the same drag should cover less of the world - otherwise
      // close inspection becomes impossible because everything flies past.
      const speed = DRAG_SPEED * (fov / START_FOV);
      targetLon -= dx * speed;
      setTargetLat(targetLat + dy * speed);
      spinVelocity = -dx * speed;
      lastX = x; lastY = y;
      touched();
    }
    function onPointerUp() { dragging = false; touched(); }

    const dom = renderer.domElement;
    dom.style.touchAction = "none";
    dom.tabIndex = 0;   // so the canvas can take keyboard focus

    const onMouseDown = (e) => { onPointerDown(e.clientX, e.clientY); dom.focus(); };
    const onMouseMove = (e) => onPointerMove(e.clientX, e.clientY);
    const onMouseUp = () => onPointerUp();
    dom.addEventListener("mousedown", onMouseDown);
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);

    let pinchDist = null;
    const onTouchStart = (e) => {
      if (e.touches.length === 1) onPointerDown(e.touches[0].clientX, e.touches[0].clientY);
      else if (e.touches.length === 2) { pinchDist = touchDist(e.touches); dragging = false; }
    };
    const onTouchMove = (e) => {
      if (e.touches.length === 1) onPointerMove(e.touches[0].clientX, e.touches[0].clientY);
      else if (e.touches.length === 2) {
        const d = touchDist(e.touches);
        if (pinchDist) zoomBy((pinchDist - d) * 0.12);
        pinchDist = d;
        touched();
      }
    };
    const onTouchEnd = () => { dragging = false; pinchDist = null; touched(); };
    dom.addEventListener("touchstart", onTouchStart, { passive: true });
    dom.addEventListener("touchmove", onTouchMove, { passive: true });
    dom.addEventListener("touchend", onTouchEnd);

    const onWheel = (e) => { e.preventDefault(); zoomBy(e.deltaY * 0.05); touched(); };
    dom.addEventListener("wheel", onWheel, { passive: false });

    // Double-click zooms toward what you clicked, the way a map does.
    const onDblClick = (e) => {
      if (moved) return;
      zoomBy(targetFov > MIN_FOV + 12 ? -22 : 22);
      touched();
      e.preventDefault();
    };
    dom.addEventListener("dblclick", onDblClick);

    const onKeyDown = (e) => {
      const step = fov / 8;
      switch (e.key) {
        case "ArrowLeft":  targetLon -= step; break;
        case "ArrowRight": targetLon += step; break;
        case "ArrowUp":    setTargetLat(targetLat + step); break;
        case "ArrowDown":  setTargetLat(targetLat - step); break;
        case "+": case "=": zoomBy(-10); break;
        case "-": case "_": zoomBy(10); break;
        case "f": case "F": toggleFullscreen(); break;
        default: return;
      }
      e.preventDefault();
      touched();
    };
    dom.addEventListener("keydown", onKeyDown);

    function touchDist(touches) {
      const dx = touches[0].clientX - touches[1].clientX;
      const dy = touches[0].clientY - touches[1].clientY;
      return Math.hypot(dx, dy);
    }
    function zoomBy(delta) {
      targetFov = Math.max(MIN_FOV, Math.min(MAX_FOV, targetFov + delta));
    }

    function toggleFullscreen() {
      const el = host.parentElement || host;
      if (document.fullscreenElement) document.exitFullscreen && document.exitFullscreen();
      else if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
    }

    // Optional device-orientation look-around.
    let orientationActive = false;
    let baseAlpha = null;
    function orientationHandler(ev) {
      if (ev.alpha == null) return;
      if (baseAlpha == null) baseAlpha = ev.alpha;
      targetLon = -(ev.alpha - baseAlpha);
      setTargetLat((ev.beta || 0) - 90);
      touched();
    }
    function enableOrientation() {
      if (typeof DeviceOrientationEvent !== "undefined" &&
          typeof DeviceOrientationEvent.requestPermission === "function") {
        DeviceOrientationEvent.requestPermission().then((res) => {
          if (res === "granted") {
            window.addEventListener("deviceorientation", orientationHandler, true);
            orientationActive = true;
          }
        }).catch(() => {});
      } else if (window.DeviceOrientationEvent) {
        window.addEventListener("deviceorientation", orientationHandler, true);
        orientationActive = true;
      }
      return orientationActive;
    }

    let running = true;
    let lastFrame = performance.now();
    const target = new THREE.Vector3();

    function animate(now) {
      if (!running) return;
      requestAnimationFrame(animate);
      const dt = Math.min(0.1, (now - lastFrame) / 1000);
      lastFrame = now;

      // A flick keeps going and slows down, rather than stopping the instant
      // the finger lifts.
      if (!dragging && Math.abs(spinVelocity) > 0.01) {
        targetLon += spinVelocity;
        spinVelocity *= FLICK_DECAY;
      } else if (!dragging) {
        spinVelocity = 0;
        if (autoRotate && !orientationActive && now - lastInteraction > IDLE_BEFORE_DRIFT_MS) {
          targetLon += DRIFT_DEG_PER_SEC * dt;
        }
      }

      lon += (targetLon - lon) * DAMPING;
      lat += (targetLat - lat) * DAMPING;
      if (Math.abs(targetFov - fov) > 0.01) {
        fov += (targetFov - fov) * DAMPING;
        camera.fov = fov;
        camera.updateProjectionMatrix();
      }

      const phi = THREE.MathUtils.degToRad(90 - lat);
      const theta = THREE.MathUtils.degToRad(lon);
      target.set(
        500 * Math.sin(phi) * Math.cos(theta),
        500 * Math.cos(phi),
        500 * Math.sin(phi) * Math.sin(theta)
      );
      camera.lookAt(target);
      renderer.render(scene, camera);
    }
    requestAnimationFrame(animate);

    function onResize() {
      camera.aspect = width() / height();
      camera.updateProjectionMatrix();
      renderer.setSize(width(), height());
    }
    window.addEventListener("resize", onResize);
    // Fullscreen and rotation change the host without firing a window resize.
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(onResize) : null;
    if (ro) ro.observe(host);

    return {
      enableOrientation,
      toggleFullscreen,
      setAutoRotate(on) { autoRotate = !!on; touched(); return autoRotate; },
      isAutoRotating() { return autoRotate; },
      zoomIn() { zoomBy(-12); touched(); },
      zoomOut() { zoomBy(12); touched(); },
      resetView() { targetLon = 0; setTargetLat(0); targetFov = START_FOV; touched(); },
      destroy() {
        running = false;
        // Named handlers throughout: the previous version passed fresh arrow
        // functions to removeEventListener, which removes nothing at all and
        // leaked a listener and a WebGL context per visit.
        window.removeEventListener("resize", onResize);
        window.removeEventListener("mousemove", onMouseMove);
        window.removeEventListener("mouseup", onMouseUp);
        window.removeEventListener("deviceorientation", orientationHandler, true);
        dom.removeEventListener("mousedown", onMouseDown);
        dom.removeEventListener("touchstart", onTouchStart);
        dom.removeEventListener("touchmove", onTouchMove);
        dom.removeEventListener("touchend", onTouchEnd);
        dom.removeEventListener("wheel", onWheel);
        dom.removeEventListener("dblclick", onDblClick);
        dom.removeEventListener("keydown", onKeyDown);
        if (ro) ro.disconnect();
        if (texture) texture.dispose();
        geometry.dispose();
        if (mesh) mesh.material.dispose();
        renderer.dispose();
        if (dom.parentNode) dom.parentNode.removeChild(dom);
      },
    };
  }

  return { create };
})();
