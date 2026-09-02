// Where does a target direction land on the screen?
//
// This is the maths behind the blue dots. Each target is a fixed direction in
// the world - "90 degrees right of where you started, level with the horizon".
// Given the phone's current rotation we work out where that direction appears
// in the camera view, and draw a dot there. Turn the phone and the dot slides
// across the screen and off the edge, exactly like Street View's capture.
const SphereMath = (() => {
  "use strict";

  const O = typeof Orientation !== "undefined" ? Orientation
          : (typeof require !== "undefined" ? require("./orientation.js") : null);

  const DEG = Math.PI / 180;
  const WORLD_UP = [0, 0, 1];   // the sensor frame is gravity-aligned: +Z is up

  function normalize(v) {
    const n = Math.hypot(v[0], v[1], v[2]);
    return n === 0 ? [0, 0, 0] : [v[0] / n, v[1] / n, v[2] / n];
  }
  function cross(a, b) {
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]];
  }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

  // The direction the camera points, in world space, for a given rotation.
  // The rear camera looks along the device's -Z axis.
  function forwardOf(q) {
    return O.qRotate(q, [0, 0, -1]);
  }

  // Build the reference frame from the rotation held at the moment the user
  // locked "Front". Everything is measured from here, so the frame only needs
  // to be gravity-aligned - it never needs to know true north.
  function referenceFrame(q0) {
    const f = forwardOf(q0);
    // Flatten onto the horizontal plane: looking up at the ceiling when you
    // press Front must still give a sane forward direction.
    let h = [f[0] - WORLD_UP[0] * dot(f, WORLD_UP),
             f[1] - WORLD_UP[1] * dot(f, WORLD_UP),
             f[2] - WORLD_UP[2] * dot(f, WORLD_UP)];
    if (Math.hypot(h[0], h[1], h[2]) < 1e-6) {
      // Pointing straight up or down: no usable heading, fall back to the
      // device's up axis projected flat.
      const u = O.qRotate(q0, [0, 1, 0]);
      h = [u[0], u[1], 0];
    }
    h = normalize(h);
    const left = normalize(cross(WORLD_UP, h));   // +90 degrees counter-clockwise
    return { forward: h, left: left, up: WORLD_UP };
  }

  // A target direction in world space. yaw is degrees CLOCKWISE from Front
  // (turning right), pitch is degrees above the horizon.
  function directionFor(frame, yawDeg, pitchDeg) {
    const y = yawDeg * DEG, p = pitchDeg * DEG;
    const cy = Math.cos(y), sy = Math.sin(y);
    // Clockwise means subtracting the left vector.
    const h = [
      frame.forward[0] * cy - frame.left[0] * sy,
      frame.forward[1] * cy - frame.left[1] * sy,
      frame.forward[2] * cy - frame.left[2] * sy,
    ];
    const cp = Math.cos(p), sp = Math.sin(p);
    return normalize([
      h[0] * cp + frame.up[0] * sp,
      h[1] * cp + frame.up[1] * sp,
      h[2] * cp + frame.up[2] * sp,
    ]);
  }

  // Focal length in pixels for a given horizontal field of view.
  function focalLength(viewWidth, hfovDeg) {
    return (viewWidth / 2) / Math.tan((hfovDeg * DEG) / 2);
  }

  // Project a world direction into screen pixels.
  //
  // Returns { x, y, visible, angle } where `angle` is how far off-centre the
  // target is in degrees - that is what decides when to fire the shutter.
  // `visible` is false when the target is behind the camera, in which case x/y
  // are meaningless and the caller should draw an edge arrow instead.
  function project(q, worldDir, view) {
    const inv = O.qConjugate(q);
    const d = O.qRotate(inv, worldDir);      // direction in device space

    // Camera looks along -Z, so anything in front has d[2] < 0.
    const depth = -d[2];
    const angle = Math.acos(Math.max(-1, Math.min(1, depth))) / DEG;

    if (depth <= 1e-4) {
      return { x: 0, y: 0, visible: false, angle: angle, behind: true };
    }
    const f = focalLength(view.width, view.hfov || 65);
    return {
      x: view.width / 2 + f * (d[0] / depth),
      // Screen Y grows downward, device +Y is up.
      y: view.height / 2 - f * (d[1] / depth),
      visible: true,
      angle: angle,
      behind: false,
    };
  }

  // Yaw/pitch of the camera relative to the reference frame. Used for the
  // readout and for recording where each photo actually pointed.
  function headingOf(frame, q) {
    const f = forwardOf(q);
    const up = dot(f, frame.up);
    const pitch = Math.asin(Math.max(-1, Math.min(1, up))) / DEG;
    const fwd = dot(f, frame.forward);
    const lft = dot(f, frame.left);
    // Negated because yaw is measured clockwise.
    let yaw = Math.atan2(-lft, fwd) / DEG;
    if (yaw < 0) yaw += 360;
    return { yaw: yaw, pitch: pitch };
  }

  return { referenceFrame, directionFor, project, headingOf, focalLength, forwardOf, normalize, cross, dot };
})();

if (typeof module !== "undefined" && module.exports) module.exports = SphereMath;
