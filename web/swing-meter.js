// Did the phone travel between two photos, or only turn?
//
// Turning on the spot is what a 360 needs. Turning your BODY with the phone
// held out swings the lens round a circle half a metre across, and near things
// then tear at the join - the fault the ring check reports after the fact.
// This catches it while shooting, from the motion sensors alone.
//
// The gyroscope says how fast the phone turns (w). Anything swung round a
// point r away feels a pull towards that point of w^2 * r, which the
// accelerometer measures. Turning on the spot, r is a few centimetres and the
// pull is nothing; swung at arm's length it is plain to read: at a gentle
// 60 degrees a second and r = 0.5 m, about 0.55 m/s^2. So r comes straight out
// of the two sensors, with no integrating and so no drift.
//
// Hand shake is acceleration too. Only samples taken while really turning are
// used, where the pull is strong, and they are summed as directions, in which
// shake cancels out (see sample()).
const SwingMeter = (() => {
  // Slower than this and the pull is lost in hand shake (rad/s; ~34 deg/s).
  const MIN_TURN = 0.6;
  // Swinging further than this between two photos is worth a word (metres).
  const WARN_TRAVEL_M = 0.10;
  // And the turning point must be at least this far out. A lens sits a few
  // centimetres from the middle of any phone, and shaky hands read as up to
  // about 0.2 m on the spot in the tests; a phone held at the chest while
  // turning the body swings at about 0.3 m, at arm's length 0.5 m or more.
  const WARN_RADIUS_M = 0.25;
  const MIN_SAMPLES = 6;

  function create() {
    const acc3 = [0, 0, 0];
    let den = 0, n = 0, turned = 0, lastT = 0;

    function reset() {
      acc3[0] = acc3[1] = acc3[2] = 0; den = 0; n = 0; turned = 0; lastT = 0;
    }

    /**
     * One devicemotion reading.
     * rate  rotationRate {alpha, beta, gamma}, degrees per second
     * acc   acceleration {x, y, z} without gravity, m/s^2
     * t     timestamp in ms
     */
    function sample(rate, acc, t) {
      if (!rate || !acc || acc.x == null || rate.alpha == null) return;
      const d = Math.PI / 180;
      // alpha turns about the screen's z axis, beta about x, gamma about y.
      const w = [rate.beta * d, rate.gamma * d, rate.alpha * d];
      const a = [acc.x, acc.y, acc.z];
      const ww = w[0] * w[0] + w[1] * w[1] + w[2] * w[2];
      const speed = Math.sqrt(ww);
      const dt = lastT ? Math.min(0.2, Math.max(0, (t - lastT) / 1000)) : 0;
      lastT = t;
      turned += speed * dt;

      if (speed < MIN_TURN) return;
      // The pull points at the turning point, square to the axis of the turn,
      // so the part of the acceleration along the axis is not it.
      const along = (a[0] * w[0] + a[1] * w[1] + a[2] * w[2]) / speed;
      // Summed as a DIRECTION, not a size. The pull always points the same way
      // in the phone's own frame - back towards the body - while shake points
      // anywhere and the push of speeding up reverses when slowing down, so
      // both cancel in the sum. Fitting sizes instead let shake alone read as
      // a 0.2 m swing, since a size is never negative and so never cancels.
      const s = ww;   // weight each sample by w^2, least squares for a = w^2 r
      for (let k = 0; k < 3; k++) acc3[k] += (a[k] - along * w[k] / speed) * s;
      den += ww * ww;
      n++;
    }

    /** What the phone did since the last reset. */
    function result() {
      const radius = n >= MIN_SAMPLES && den > 0 ? Math.hypot(acc3[0], acc3[1], acc3[2]) / den : 0;
      // The lens travels the chord of the arc it swung through.
      const travel = 2 * radius * Math.sin(Math.min(turned, Math.PI) / 2);
      return {
        radius, travel, turnedDeg: turned * 180 / Math.PI, samples: n,
        swung: radius >= WARN_RADIUS_M && travel >= WARN_TRAVEL_M,
      };
    }

    return { sample, reset, result };
  }

  return { create, WARN_TRAVEL_M, WARN_RADIUS_M };
})();

if (typeof module !== "undefined") module.exports = SwingMeter;
