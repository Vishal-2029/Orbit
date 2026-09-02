// Equirectangular panorama viewer on an inverted three.js sphere.
// Supports drag-to-look, wheel/pinch zoom, and optional device-orientation look-around.
const SphereViewer = (() => {
  function create(host, panoramaUrl, onLoadProgress) {
    const width = () => host.clientWidth;
    const height = () => host.clientHeight;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, width() / height(), 0.1, 1000);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(width(), height());
    host.appendChild(renderer.domElement);

    const geometry = new THREE.SphereGeometry(500, 60, 40);
    geometry.scale(-1, 1, 1); // invert so texture renders on the inside

    let mesh = null;
    const loader = new THREE.TextureLoader();
    loader.load(
      panoramaUrl,
      (texture) => {
        const material = new THREE.MeshBasicMaterial({ map: texture });
        mesh = new THREE.Mesh(geometry, material);
        scene.add(mesh);
        onLoadProgress && onLoadProgress(1);
      },
      (xhr) => {
        if (xhr.total) onLoadProgress && onLoadProgress(xhr.loaded / xhr.total);
      },
      (err) => {
        onLoadProgress && onLoadProgress(-1, err);
      }
    );

    let lon = 0, lat = 0;
    let dragging = false, lastX = 0, lastY = 0;
    let fov = 75;

    function onPointerDown(x, y) { dragging = true; lastX = x; lastY = y; }
    function onPointerMove(x, y) {
      if (!dragging) return;
      lon -= (x - lastX) * 0.15;
      lat += (y - lastY) * 0.15;
      lat = Math.max(-85, Math.min(85, lat));
      lastX = x; lastY = y;
    }
    function onPointerUp() { dragging = false; }

    const dom = renderer.domElement;
    dom.style.touchAction = "none";
    dom.addEventListener("mousedown", (e) => onPointerDown(e.clientX, e.clientY));
    window.addEventListener("mousemove", (e) => onPointerMove(e.clientX, e.clientY));
    window.addEventListener("mouseup", onPointerUp);

    let pinchDist = null;
    dom.addEventListener("touchstart", (e) => {
      if (e.touches.length === 1) {
        onPointerDown(e.touches[0].clientX, e.touches[0].clientY);
      } else if (e.touches.length === 2) {
        pinchDist = touchDist(e.touches);
      }
    }, { passive: true });
    dom.addEventListener("touchmove", (e) => {
      if (e.touches.length === 1) {
        onPointerMove(e.touches[0].clientX, e.touches[0].clientY);
      } else if (e.touches.length === 2) {
        const d = touchDist(e.touches);
        if (pinchDist) {
          fov = clampFov(fov - (d - pinchDist) * 0.05);
          camera.fov = fov;
          camera.updateProjectionMatrix();
        }
        pinchDist = d;
      }
    }, { passive: true });
    dom.addEventListener("touchend", () => { dragging = false; pinchDist = null; });

    dom.addEventListener("wheel", (e) => {
      e.preventDefault();
      fov = clampFov(fov + e.deltaY * 0.03);
      camera.fov = fov;
      camera.updateProjectionMatrix();
    }, { passive: false });

    function touchDist(touches) {
      const dx = touches[0].clientX - touches[1].clientX;
      const dy = touches[0].clientY - touches[1].clientY;
      return Math.sqrt(dx * dx + dy * dy);
    }
    function clampFov(v) { return Math.max(30, Math.min(100, v)); }

    // Optional device-orientation look-around.
    let orientationActive = false;
    let baseAlpha = null;
    function orientationHandler(ev) {
      if (ev.alpha == null) return;
      if (baseAlpha == null) baseAlpha = ev.alpha;
      lon = -(ev.alpha - baseAlpha);
      lat = Math.max(-85, Math.min(85, (ev.beta || 0) - 90));
    }
    function enableOrientation() {
      if (typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission === "function") {
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
    function animate() {
      if (!running) return;
      requestAnimationFrame(animate);
      const phi = THREE.MathUtils.degToRad(90 - lat);
      const theta = THREE.MathUtils.degToRad(lon);
      const target = new THREE.Vector3(
        500 * Math.sin(phi) * Math.cos(theta),
        500 * Math.cos(phi),
        500 * Math.sin(phi) * Math.sin(theta)
      );
      camera.lookAt(target);
      renderer.render(scene, camera);
    }
    animate();

    function onResize() {
      camera.aspect = width() / height();
      camera.updateProjectionMatrix();
      renderer.setSize(width(), height());
    }
    window.addEventListener("resize", onResize);

    return {
      enableOrientation,
      destroy() {
        running = false;
        window.removeEventListener("resize", onResize);
        window.removeEventListener("deviceorientation", orientationHandler, true);
        window.removeEventListener("mousemove", (e) => onPointerMove(e.clientX, e.clientY));
        window.removeEventListener("mouseup", onPointerUp);
        renderer.dispose();
        if (dom.parentNode) dom.parentNode.removeChild(dom);
      },
    };
  }

  return { create };
})();
