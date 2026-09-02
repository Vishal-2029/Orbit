// Device orientation, the way a photo-sphere app actually needs it.
//
// The old code read DeviceOrientationEvent.alpha, which is derived from the
// MAGNETOMETER. Indoors that is wrong by tens of degrees - steel, wiring and
// monitors all pull it around - which is why the guidance arrows never lined up.
//
// Google's Street View capture avoids the magnetometer for exactly this reason
// and fuses the GYROSCOPE with the accelerometer instead. That is what
// RelativeOrientationSensor exposes: a gravity-aligned quaternion, updated at
// 60Hz, accurate to a fraction of a degree over the ~30 seconds a capture takes.
//
// Sources are tried best-first:
//   1. RelativeOrientationSensor  - gyro + accelerometer. No compass, no
//                                   magnetic interference. Chrome/Android 69+.
//   2. AbsoluteOrientationSensor  - adds the magnetometer for true north.
//                                   Only used if the relative one is missing.
//   3. DeviceOrientationEvent     - the legacy Euler-angle API. iOS Safari has
//                                   nothing else, so it stays as the fallback.
//
// Everything downstream sees one shape: a quaternion plus a source label.
const Orientation = (() => {
  "use strict";

  // --- quaternion helpers -------------------------------------------------
  // Quaternions are [x, y, z, w], matching the Generic Sensor API's layout.

  function qNormalize(q) {
    const n = Math.hypot(q[0], q[1], q[2], q[3]);
    return n === 0 ? [0, 0, 0, 1] : [q[0] / n, q[1] / n, q[2] / n, q[3] / n];
  }

  function qConjugate(q) {
    return [-q[0], -q[1], -q[2], q[3]];
  }

  // Rotate vector v by quaternion q.
  function qRotate(q, v) {
    const [x, y, z, w] = q;
    const [vx, vy, vz] = v;
    // t = 2 * (q_vec x v)
    const tx = 2 * (y * vz - z * vy);
    const ty = 2 * (z * vx - x * vz);
    const tz = 2 * (x * vy - y * vx);
    // v' = v + w*t + q_vec x t
    return [
      vx + w * tx + (y * tz - z * ty),
      vy + w * ty + (z * tx - x * tz),
      vz + w * tz + (x * ty - y * tx),
    ];
  }

  // Build a quaternion from the legacy alpha/beta/gamma Euler angles.
  // The order is Z-X'-Y'' as specified for DeviceOrientationEvent.
  function quaternionFromEuler(alphaDeg, betaDeg, gammaDeg) {
    const d2r = Math.PI / 180;
    const a = (alphaDeg || 0) * d2r, b = (betaDeg || 0) * d2r, g = (gammaDeg || 0) * d2r;
    const ca = Math.cos(a / 2), sa = Math.sin(a / 2);
    const cb = Math.cos(b / 2), sb = Math.sin(b / 2);
    const cg = Math.cos(g / 2), sg = Math.sin(g / 2);
    return qNormalize([
      sb * cg * ca - cb * sg * sa,   // x
      cb * sg * ca + sb * cg * sa,   // y
      cb * cg * sa + sb * sg * ca,   // z
      cb * cg * ca - sb * sg * sa,   // w
    ]);
  }

  // The sensor reports in the DEVICE frame. Held in landscape, that frame is
  // rotated relative to what the user sees, so every projected point would be
  // rotated too. This folds the screen rotation back out.
  function applyScreenRotation(q, screenAngleDeg) {
    if (!screenAngleDeg) return q;
    const half = (-screenAngleDeg * Math.PI) / 360; // (-angle/2) in radians
    const zRot = [0, 0, Math.sin(half), Math.cos(half)];
    return qNormalize(qMultiply(q, zRot));
  }

  function qMultiply(a, b) {
    const [ax, ay, az, aw] = a;
    const [bx, by, bz, bw] = b;
    return [
      aw * bx + ax * bw + ay * bz - az * by,
      aw * by - ax * bz + ay * bw + az * bx,
      aw * bz + ax * by - ay * bx + az * bw,
      aw * bw - ax * bx - ay * by - az * bz,
    ];
  }

  function screenAngle() {
    if (typeof screen !== "undefined" && screen.orientation &&
        typeof screen.orientation.angle === "number") {
      return screen.orientation.angle;
    }
    return (typeof window !== "undefined" && window.orientation) || 0;
  }

  // --- the tracker --------------------------------------------------------

  function create() {
    const state = {
      quaternion: null,   // [x,y,z,w] with screen rotation already folded in
      source: "none",     // gyro | absolute | deviceorientation | none
      lastUpdate: 0,
      error: null,
    };

    let sensor = null;
    let listener = null;
    let started = false;

    function setQuat(q) {
      state.quaternion = applyScreenRotation(qNormalize(q), screenAngle());
      state.lastUpdate = Date.now();
    }

    function startGenericSensor(Ctor, label) {
      try {
        sensor = new Ctor({ frequency: 60, referenceFrame: "device" });
        sensor.addEventListener("reading", () => {
          if (sensor.quaternion) {
            setQuat(sensor.quaternion);
            state.source = label;
          }
        });
        sensor.addEventListener("error", (ev) => {
          // NotAllowedError / NotReadableError land here; drop to the legacy API
          // rather than leaving the user with a dead screen.
          state.error = (ev.error && ev.error.name) || "sensor error";
          sensor = null;
          startLegacy();
        });
        sensor.start();
        return true;
      } catch (e) {
        state.error = e.name || String(e);
        sensor = null;
        return false;
      }
    }

    function startLegacy() {
      if (listener || typeof window === "undefined" || !window.DeviceOrientationEvent) return;
      listener = (ev) => {
        if (ev.alpha == null && ev.beta == null && ev.gamma == null) return;
        setQuat(quaternionFromEuler(ev.alpha, ev.beta, ev.gamma));
        state.source = "deviceorientation";
      };
      window.addEventListener("deviceorientationabsolute", listener, true);
      window.addEventListener("deviceorientation", listener, true);
    }

    // iOS needs an explicit permission grant from inside a user gesture.
    async function requestPermission() {
      if (typeof DeviceOrientationEvent !== "undefined" &&
          typeof DeviceOrientationEvent.requestPermission === "function") {
        try {
          return (await DeviceOrientationEvent.requestPermission()) === "granted";
        } catch (e) {
          return false;
        }
      }
      return true;
    }

    async function start() {
      if (started) return state.source;
      started = true;

      const ok = await requestPermission();
      if (!ok) {
        state.error = "permission denied";
        return state.source;
      }

      // Prefer the gyroscope-only source: no magnetometer means no indoor drift.
      if (typeof window !== "undefined" && window.RelativeOrientationSensor) {
        if (startGenericSensor(window.RelativeOrientationSensor, "gyro")) return "gyro";
      }
      if (typeof window !== "undefined" && window.AbsoluteOrientationSensor) {
        if (startGenericSensor(window.AbsoluteOrientationSensor, "absolute")) return "absolute";
      }
      startLegacy();
      return state.source;
    }

    function stop() {
      if (sensor) { try { sensor.stop(); } catch (e) {} sensor = null; }
      if (listener) {
        window.removeEventListener("deviceorientationabsolute", listener, true);
        window.removeEventListener("deviceorientation", listener, true);
        listener = null;
      }
      started = false;
    }

    function isLive(maxAgeMs = 1500) {
      return state.quaternion != null && (Date.now() - state.lastUpdate) < maxAgeMs;
    }

    return {
      start, stop, isLive,
      get quaternion() { return state.quaternion; },
      get source() { return state.source; },
      get error() { return state.error; },
      get lastUpdate() { return state.lastUpdate; },
    };
  }

  return {
    create,
    qNormalize, qConjugate, qRotate, qMultiply,
    quaternionFromEuler, applyScreenRotation,
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Orientation;
