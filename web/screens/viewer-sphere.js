// Equirectangular panorama viewer, on OrbitPano.
//
// This replaced a hand-written three.js sphere. The sphere itself was never the
// problem - it worked - but everything we wanted to build on top of it needs two
// pieces of maths the engine already has and three.js does not:
//
//   view.coordinatesToScreen({yaw, pitch})  ->  where to draw a marker
//   view.screenToCoordinates({x, y})        ->  what the user just clicked on
//
// The second one IS the hotspot editor. Without it, placing a marker by clicking
// means writing sphere raycasting by hand, and then writing it again the first
// time the projection changes.
//
// The engine also reads a plain equirectangular JPEG, so the panorama the worker
// already produces drops straight in with no tiling pipeline. When
// manifest.tiles is present it uses cube tiles instead, which stay sharp when
// zoomed - see CubeGeometry below.
//
// The old viewer is still in viewer-sphere-three.js behind window.ORBIT_VIEWER.
const SphereViewer = (() => {
  "use strict";

  const DEG = Math.PI / 180;

  // Field of view, in radians. The engine thinks in radians throughout and so
  // does the hotspot data, so nothing here converts back and forth.
  const MIN_FOV = 25 * DEG;
  const MAX_FOV = 110 * DEG;
  const START_FOV = 75 * DEG;
  const ZOOM_STEP = 12 * DEG;

  // A panorama that sits perfectly still reads as a flat photo rather than
  // somewhere you are standing, so it drifts once you stop touching it.
  const IDLE_BEFORE_DRIFT_MS = 4000;
  const DRIFT_RAD_PER_SEC = 1.8 * DEG;

  /**
   * host      element to fill
   * scenes    [{ id, title, panorama, tiles, faceSize, initialView, hotspots }]
   * opts      { onProgress, onSceneChange, onHotspotClick, startSceneId,
   *             autoRotate, editable, onPlace }
   */
  function create(host, scenes, opts) {
    const options = opts || {};
    const onProgress = options.onProgress || function () {};
    // Not read from options each time: edit mode is toggled long after the
    // viewer is built, and the hotspot elements are rebuilt when it changes.
    let editable = !!options.editable;

    if (typeof OrbitPano === "undefined") {
      onProgress(-1, new Error("The 360 viewer failed to load."));
      return null;
    }
    if (!scenes || !scenes.length) {
      onProgress(-1, new Error("There is nothing to show."));
      return null;
    }

    const viewer = new OrbitPano.Viewer(host, {
      controls: { mouseViewMode: "drag" },
      // The engine puts its own hotspot layer inside this element, so it has to
      // be the positioned ancestor everything else measures against.
      stage: { preserveDrawingBuffer: false },
    });

    // Zoom is capped by the source resolution as well as by taste: letting
    // someone zoom past the pixels just shows them the blur.
    function limiterFor(scene) {
      const maxRes = scene.tiles ? (scene.faceSize || 4096) : 4096;
      return OrbitPano.util.compose(
        OrbitPano.RectilinearView.limit.vfov(MIN_FOV, MAX_FOV),
        OrbitPano.RectilinearView.limit.hfov(MIN_FOV, MAX_FOV),
        OrbitPano.RectilinearView.limit.pitch(-Math.PI / 2, Math.PI / 2),
        OrbitPano.RectilinearView.limit.resolution(maxRes)
      );
    }

    function geometryFor(scene) {
      if (scene.tiles) {
        // Multi-resolution cube faces. levels come from the manifest so the
        // viewer never has to guess what the worker produced.
        return new OrbitPano.CubeGeometry(scene.levels);
      }
      // A single equirectangular image. width is what the engine uses to decide
      // when it has enough pixels, not a promise about the file.
      return new OrbitPano.EquirectGeometry([{ width: scene.width || 4096 }]);
    }

    function sourceFor(scene) {
      if (scene.tiles) {
        return OrbitPano.ImageUrlSource.fromString(scene.tiles, {
          cubeMapPreviewUrl: scene.preview || undefined,
        });
      }
      return OrbitPano.ImageUrlSource.fromString(scene.panorama);
    }

    // ----------------------------------------------------------------------
    // Build every scene up front.
    //
    // The engine only downloads tiles for the scene it is showing, so this is
    // cheap, and having them ready is what lets a link hotspot cross-fade
    // instead of tearing the viewer down and rebuilding it.
    // ----------------------------------------------------------------------
    const built = scenes.map((s) => {
      const view = new OrbitPano.RectilinearView(
        {
          yaw: (s.initialView && s.initialView.yaw) || 0,
          pitch: (s.initialView && s.initialView.pitch) || 0,
          fov: (s.initialView && s.initialView.fov) || START_FOV,
        },
        limiterFor(s)
      );
      const scene = viewer.createScene({
        source: sourceFor(s),
        geometry: geometryFor(s),
        view: view,
        pinFirstLevel: true,
      });
      return { data: s, scene: scene, view: view, hotspots: [] };
    });

    const byId = {};
    built.forEach((b) => (byId[b.data.id] = b));

    let current = (options.startSceneId && byId[options.startSceneId]) || built[0];

    // ----------------------------------------------------------------------
    // Hotspots
    // ----------------------------------------------------------------------
    function attachHotspots(entry) {
      // The engine positions whatever DOM element we hand it, every frame, and
      // hides it when it goes behind the camera. All we supply is the markup.
      (entry.data.hotspots || []).forEach((h) => {
        const el = Hotspots.createElement(h, {
          onActivate: () => {
            if (h.kind === "link") {
              switchTo(h.target_scene_id || h.target_capture_id);
            }
            options.onHotspotClick && options.onHotspotClick(h);
          },
          editable: editable,
          onEdit: options.onEdit,
          onDelete: options.onDelete,
        });
        entry.scene.hotspotContainer().createHotspot(el, {
          yaw: h.yaw,
          pitch: h.pitch,
        });
        entry.hotspots.push({ data: h, element: el });
      });
    }
    built.forEach(attachHotspots);

    function reloadHotspots(sceneId, hotspots) {
      const entry = byId[sceneId];
      if (!entry) return;
      const container = entry.scene.hotspotContainer();
      container.listHotspots().forEach((hs) => container.destroyHotspot(hs));
      entry.hotspots.length = 0;
      entry.data.hotspots = hotspots || [];
      attachHotspots(entry);
    }

    // ----------------------------------------------------------------------
    // Autorotate, paused while the user is doing anything
    // ----------------------------------------------------------------------
    const autorotate = OrbitPano.autorotate({
      yawSpeed: DRIFT_RAD_PER_SEC,
      targetPitch: 0,
      targetFov: START_FOV,
    });
    let autoRotate = options.autoRotate !== false;
    let lastInteraction = performance.now();

    function touched() {
      lastInteraction = performance.now();
      viewer.stopMovement();
      viewer.setIdleMovement(IDLE_BEFORE_DRIFT_MS, autoRotate ? autorotate : null);
    }
    if (autoRotate) {
      viewer.setIdleMovement(IDLE_BEFORE_DRIFT_MS, autorotate);
    }

    // ----------------------------------------------------------------------
    // Loading progress
    //
    // There is no byte-level progress for a single equirect, so this reports the
    // two states the engine does know: the first level is in, or it timed out.
    // ----------------------------------------------------------------------
    let readyReported = false;
    function reportWhenReady() {
      const stage = viewer.stage();
      function onRender(stable) {
        // renderComplete carries a flag saying whether every visible tile was
        // ready. Waiting for it is what stops the loader clearing over a
        // half-drawn sphere.
        if (!stable || readyReported) return;
        readyReported = true;
        stage.removeEventListener("renderComplete", onRender);
        onProgress(1);
      }
      stage.addEventListener("renderComplete", onRender);
      // A panorama that will not load must not leave a spinner up forever.
      setTimeout(() => {
        if (readyReported) return;
        readyReported = true;
        stage.removeEventListener("renderComplete", onRender);
        onProgress(1);
      }, 15000);
    }

    current.scene.switchTo({ transitionDuration: 0 });
    reportWhenReady();
    options.onSceneChange && options.onSceneChange(current.data);

    // ----------------------------------------------------------------------
    // Scene switching
    // ----------------------------------------------------------------------
    function switchTo(sceneId, transitionMs) {
      const next = byId[sceneId];
      if (!next || next === current) return false;
      current = next;
      next.scene.switchTo({
        transitionDuration: transitionMs == null ? 700 : transitionMs,
      });
      touched();
      options.onSceneChange && options.onSceneChange(next.data);
      return true;
    }

    // ----------------------------------------------------------------------
    // Placing a hotspot: the click-to-coordinates half of the editor
    // ----------------------------------------------------------------------
    // A click here means "put a hotspot there", so it has to be told apart
    // from a drag - otherwise every time the user looks around they drop a
    // marker. Track where the pointer went down and only count it if it barely
    // moved.
    const CLICK_SLOP_PX = 6;
    let downX = 0, downY = 0, downAt = 0;

    function onPointerDown(ev) {
      downX = ev.clientX;
      downY = ev.clientY;
      downAt = performance.now();
    }

    function onPointerUp(ev) {
      if (!editable || !options.onPlace) return;
      // Let a hotspot's own click handler have it.
      if (ev.target.closest && ev.target.closest(".hotspot")) return;
      const moved = Math.hypot(ev.clientX - downX, ev.clientY - downY);
      if (moved > CLICK_SLOP_PX || performance.now() - downAt > 700) return;

      const rect = host.getBoundingClientRect();
      const coords = current.view.screenToCoordinates({
        x: ev.clientX - rect.left,
        y: ev.clientY - rect.top,
      });
      options.onPlace({
        sceneId: current.data.id,
        yaw: coords.yaw,
        pitch: coords.pitch,
      });
    }
    host.addEventListener("pointerdown", onPointerDown);
    host.addEventListener("pointerup", onPointerUp);

    // ----------------------------------------------------------------------
    // Device orientation
    // ----------------------------------------------------------------------
    let orientationMethod = null;
    let orientationActive = false;

    function enableOrientation() {
      if (orientationActive) return true;
      const start = () => {
        const controls = viewer.controls();
        orientationMethod = new DeviceOrientationControl(current.view);
        controls.registerMethod("deviceOrientation", orientationMethod);
        controls.enableMethod("deviceOrientation");
        orientationActive = true;
        setAutoRotate(false);
      };
      if (typeof DeviceOrientationEvent !== "undefined" &&
          typeof DeviceOrientationEvent.requestPermission === "function") {
        DeviceOrientationEvent.requestPermission()
          .then((res) => { if (res === "granted") start(); })
          .catch(() => {});
        // iOS answers asynchronously, so the caller cannot be told yet.
        return false;
      }
      if (window.DeviceOrientationEvent) {
        start();
        return true;
      }
      return false;
    }

    function toggleFullscreen() {
      const el = host.parentElement || host;
      if (document.fullscreenElement) {
        document.exitFullscreen && document.exitFullscreen();
      } else if (el.requestFullscreen) {
        el.requestFullscreen().catch(() => {});
      }
    }

    function setAutoRotate(on) {
      autoRotate = !!on;
      viewer.setIdleMovement(IDLE_BEFORE_DRIFT_MS, autoRotate ? autorotate : null);
      if (!autoRotate) viewer.stopMovement();
      return autoRotate;
    }

    function zoomBy(delta) {
      const v = current.view;
      v.setFov(Math.max(MIN_FOV, Math.min(MAX_FOV, v.fov() + delta)));
      touched();
    }

    return {
      // --- the interface the old three.js viewer exposed, unchanged ---
      enableOrientation,
      toggleFullscreen,
      setAutoRotate,
      isAutoRotating() { return autoRotate; },
      zoomIn() { zoomBy(-ZOOM_STEP); },
      zoomOut() { zoomBy(ZOOM_STEP); },
      resetView() {
        const init = current.data.initialView || {};
        current.view.setYaw(init.yaw || 0);
        current.view.setPitch(init.pitch || 0);
        current.view.setFov(init.fov || START_FOV);
        touched();
      },

      // --- new ---
      switchTo,
      /** Turn placing and the per-hotspot edit controls on or off.
       *  The caller reloads hotspots afterwards; their markup differs. */
      setEditable(on) { editable = !!on; return editable; },
      isEditable() { return editable; },
      currentSceneId() { return current.data.id; },
      scenes() { return built.map((b) => b.data); },
      reloadHotspots,
      /** Where the user is looking now - used to seed a new hotspot's angle. */
      lookDirection() {
        return { yaw: current.view.yaw(), pitch: current.view.pitch() };
      },
      /** Point the view at a direction. Radians, pitch positive downwards. */
      lookAt(yaw, pitch) {
        if (typeof yaw === "number") current.view.setYaw(yaw);
        if (typeof pitch === "number") current.view.setPitch(pitch);
        touched();
      },

      destroy() {
        host.removeEventListener("pointerdown", onPointerDown);
        host.removeEventListener("pointerup", onPointerUp);
        if (orientationMethod) {
          try {
            viewer.controls().unregisterMethod("deviceOrientation");
          } catch (_) {}
          orientationMethod.destroy();
        }
        viewer.destroy();
      },
    };
  }

  return { create };
})();


// Look around by moving the phone.
//
// The engine ships no device-orientation control, only a demo of one. This is
// that demo reduced to what Orbit needs: absolute alpha/beta/gamma turned into
// yaw and pitch, with the first reading taken as the origin so the panorama
// does not jump when it is switched on.
function DeviceOrientationControl(view) {
  this._view = view;
  this._dynamics = null;
  this._origin = null;
  this._handler = this._onDeviceOrientation.bind(this);
  window.addEventListener("deviceorientation", this._handler, true);
}

DeviceOrientationControl.prototype.destroy = function () {
  window.removeEventListener("deviceorientation", this._handler, true);
  this._view = null;
};

DeviceOrientationControl.prototype._onDeviceOrientation = function (ev) {
  if (ev.alpha == null || !this._view) return;
  const DEG = Math.PI / 180;
  if (this._origin == null) this._origin = ev.alpha;

  // alpha grows anticlockwise seen from above; panorama yaw grows the other
  // way, hence the minus. It is measured from the first reading rather than
  // from north, so switching this on does not make the view jump.
  this._view.setYaw(-(ev.alpha - this._origin) * DEG);

  // beta is 90 with the phone held upright facing the horizon, and grows as
  // the top of the phone tilts away from you - which points the REAR camera
  // upwards.
  //
  // The engine's pitch is positive DOWNWARDS: screenToCoordinates maps the top
  // of the screen to a negative pitch. So tilting up has to produce a negative
  // pitch, and the minus here is the whole reason this comment exists - without
  // it, raising the phone looked at the floor.
  //
  // Checked against the engine's own device-orientation conversion, which
  // returns -0.349 rad for beta=110 and +0.349 for beta=70.
  const pitch = -((ev.beta || 0) - 90) * DEG;
  this._view.setPitch(Math.max(-Math.PI / 2, Math.min(Math.PI / 2, pitch)));
};

// The engine's control registry expects these, even for a method that only
// writes to the view directly.
DeviceOrientationControl.prototype.start = function () {};
DeviceOrientationControl.prototype.stop = function () {};
