// The photos as FRAMES around you, in the positions they will be built at.
//
// The flat map answers "where is Photo 14" precisely, and badly: it is a
// rectangle on a grid, stretched at the poles, nothing like standing in the
// room. This is the other half - the same photos hung in space at the angles
// they were shot from, seen from the middle, the way a model is looked at.
// Turn and you see which photo covers which part of the world, where two
// overlap, and where nothing was shot at all.
//
// It shows the PHOTOS, not the stitch. Nothing here is blended and no seam is
// cut: each frame is its own picture, floating where its camera pointed.
const Arrange3D = (() => {
  // Where the frames hang. Any radius draws the same picture from the centre;
  // this one keeps the near plane and the labels comfortable.
  const RADIUS = 10;

  // A photo's own angular width, in degrees, matching the flat map's
  // assumption. Only the drawn size depends on it.
  const HFOV = 63;

  function labelTexture(text, locked) {
    const c = document.createElement("canvas");
    c.width = 256; c.height = 64;
    const x = c.getContext("2d");
    x.fillStyle = "rgba(10,12,18,.85)";
    x.fillRect(0, 0, c.width, c.height);
    x.fillStyle = locked ? "#35d07f" : "#ffd84d";
    x.font = "700 34px system-ui, sans-serif";
    x.textAlign = "center";
    x.textBaseline = "middle";
    x.fillText(text, c.width / 2, c.height / 2);
    const t = new THREE.Texture(c);
    t.needsUpdate = true;
    return t;
  }

  /**
   * items  the arrange screen's photos: { n, yaw, pitch, roll, locked, img }
   *        - yaw/pitch/roll in radians, the viewer's frame
   * opts   { onSelect(item), isVisible(item), isFocused(item) }
   */
  function create(host, items, opts = {}) {
    if (typeof THREE === "undefined") return null;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b0d12);

    const camera = new THREE.PerspectiveCamera(75, 1, 0.1, 100);
    camera.position.set(0, 0, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    host.appendChild(renderer.domElement);

    // The ground and sky lines, so "up" is obvious when every photo is dark.
    const horizon = new THREE.LineLoop(
      new THREE.RingGeometry(RADIUS - 0.02, RADIUS, 96).rotateX(Math.PI / 2),
      new THREE.LineBasicMaterial({ color: 0x35406a }));
    scene.add(horizon);

    const group = new THREE.Group();
    scene.add(group);

    const meshes = [];
    items.forEach((it) => {
      // A photo's frame, sized by its angular width at this radius, hung along
      // the direction its camera pointed and turned to face the middle.
      const w = 2 * RADIUS * Math.tan(THREE.MathUtils.degToRad(HFOV) / 2);
      const ratio = (it.img && it.img.naturalHeight && it.img.naturalWidth)
        ? it.img.naturalHeight / it.img.naturalWidth : 4 / 3;
      const geo = new THREE.PlaneGeometry(w, w * ratio);
      const tex = it.img && it.img.complete && it.img.naturalWidth
        ? new THREE.Texture(it.img) : null;
      if (tex) tex.needsUpdate = true;
      const mat = new THREE.MeshBasicMaterial({
        map: tex, color: tex ? 0xffffff : 0x1e2430,
        side: THREE.DoubleSide, transparent: true, opacity: 0.9,
      });
      const mesh = new THREE.Mesh(geo, mat);

      const edge = new THREE.LineSegments(
        new THREE.EdgesGeometry(geo),
        new THREE.LineBasicMaterial({ color: 0xffffff }));
      mesh.add(edge);

      const label = new THREE.Sprite(new THREE.SpriteMaterial({
        map: labelTexture(String(it.n), it.locked), depthTest: false,
      }));
      label.scale.set(w * 0.34, w * 0.34 * 0.25, 1);
      label.position.set(0, (w * ratio) / 2 + w * 0.12, 0.02);
      mesh.add(label);

      mesh.userData = { item: it, edge, label };
      group.add(mesh);
      meshes.push(mesh);
    });

    // Where a photo hangs, from the position it was shot at. Same convention as
    // everywhere else: yaw 0 ahead and growing rightwards, pitch positive down.
    function place(mesh) {
      const it = mesh.userData.item;
      const t = it.pitch + Math.PI / 2;           // polar angle from straight up
      const dir = new THREE.Vector3(
        Math.sin(t) * Math.sin(it.yaw),
        Math.cos(t),                              // three.js has +Y up
        -Math.sin(t) * Math.cos(it.yaw));
      mesh.position.copy(dir.clone().multiplyScalar(RADIUS));
      mesh.lookAt(0, 0, 0);
      mesh.rotateZ(-it.roll);
    }

    function refresh() {
      meshes.forEach((mesh) => {
        const it = mesh.userData.item;
        const shown = opts.isVisible ? opts.isVisible(it) : true;
        const focused = opts.isFocused ? opts.isFocused(it) : false;
        mesh.visible = shown || focused;
        place(mesh);
        if (!mesh.material.map && it.img && it.img.complete && it.img.naturalWidth) {
          const t = new THREE.Texture(it.img);
          t.needsUpdate = true;
          mesh.material.map = t;
          mesh.material.color.setHex(0xffffff);
        }
        mesh.material.opacity = focused ? 1 : 0.32;
        mesh.userData.edge.material.color.setHex(
          focused ? 0xffd84d : it.locked ? 0x35d07f : 0x5a6478);
        mesh.userData.label.material.map = labelTexture(String(it.n), it.locked);
        mesh.userData.label.material.needsUpdate = true;
        mesh.renderOrder = focused ? 2 : 1;
      });
      render();
    }

    // Looking around from the middle: drag turns the head, wheel zooms.
    let yaw = 0, pitch = 0, fov = 75, dragging = null;

    function render() {
      camera.fov = fov;
      camera.updateProjectionMatrix();
      const t = pitch + Math.PI / 2;
      camera.lookAt(
        Math.sin(t) * Math.sin(yaw) * 5,
        Math.cos(t) * 5,
        -Math.sin(t) * Math.cos(yaw) * 5);
      renderer.render(scene, camera);
    }

    function point(ev) {
      const p = ev.touches ? ev.touches[0] : ev;
      return { x: p.clientX, y: p.clientY };
    }

    function onDown(ev) {
      dragging = Object.assign(point(ev), { moved: false });
      ev.preventDefault();
    }
    function onMove(ev) {
      if (!dragging) return;
      const p = point(ev);
      const dx = p.x - dragging.x, dy = p.y - dragging.y;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragging.moved = true;
      yaw -= dx * 0.005;
      pitch = Math.max(-Math.PI / 2 + 0.01, Math.min(Math.PI / 2 - 0.01, pitch + dy * 0.005));
      dragging.x = p.x; dragging.y = p.y;
      render();
      ev.preventDefault();
    }
    function onUp(ev) {
      if (dragging && !dragging.moved && opts.onSelect) {
        // A tap picks the photo under it, so the model can be used to choose
        // which one to work on next.
        const rect = renderer.domElement.getBoundingClientRect();
        const p = ev.changedTouches ? ev.changedTouches[0] : ev;
        const mouse = new THREE.Vector2(
          ((p.clientX - rect.left) / rect.width) * 2 - 1,
          -((p.clientY - rect.top) / rect.height) * 2 + 1);
        const ray = new THREE.Raycaster();
        ray.setFromCamera(mouse, camera);
        const hit = ray.intersectObjects(meshes.filter((m) => m.visible), false)[0];
        if (hit) opts.onSelect(hit.object.userData.item);
      }
      dragging = null;
    }
    function onWheel(ev) {
      fov = Math.max(30, Math.min(100, fov + Math.sign(ev.deltaY) * 4));
      render();
      ev.preventDefault();
    }

    const el = renderer.domElement;
    el.addEventListener("pointerdown", onDown);
    el.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    el.addEventListener("touchstart", onDown, { passive: false });
    el.addEventListener("touchmove", onMove, { passive: false });
    window.addEventListener("touchend", onUp);
    el.addEventListener("wheel", onWheel, { passive: false });

    function resize() {
      const w = host.clientWidth || 640;
      const h = Math.max(320, Math.round(w * 0.5));
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      render();
    }

    /** Turn the view to face one photo, so "show me Photo 14" is answerable. */
    function lookAtItem(it) {
      yaw = it.yaw;
      pitch = it.pitch;
      render();
    }

    resize();
    refresh();

    return {
      refresh, resize, lookAtItem,
      destroy() {
        el.removeEventListener("pointerdown", onDown);
        el.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        el.removeEventListener("touchstart", onDown);
        el.removeEventListener("touchmove", onMove);
        window.removeEventListener("touchend", onUp);
        el.removeEventListener("wheel", onWheel);
        meshes.forEach((m) => {
          m.geometry.dispose();
          if (m.material.map) m.material.map.dispose();
          m.material.dispose();
        });
        renderer.dispose();
        if (el.parentNode) el.parentNode.removeChild(el);
      },
    };
  }

  return { create };
})();
