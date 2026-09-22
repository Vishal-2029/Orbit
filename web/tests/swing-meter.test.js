// Swing meter: can it tell turning on the spot from swinging at arm's length?
// Run with:  node web/tests/swing-meter.test.js
const SwingMeter = require('../swing-meter.js');
let pass = 0, fail = 0;
function ok(name, cond, extra = '') { if (cond) { pass++; } else { fail++; console.log('  FAIL:', name, extra); } }

// A seeded random, so a failure can be run again.
function rng(seed) { let s = seed >>> 0; return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; }; }
function gauss(r) { return Math.sqrt(-2 * Math.log(r() + 1e-12)) * Math.cos(2 * Math.PI * r()); }

// Hold still, then turn `deg` about the phone's upright axis (a person turning
// with the phone held upright), then hold still again. The lens is `radius`
// metres from the point it turns about, so it feels w^2 * radius towards it,
// plus the push of speeding up and slowing down, plus hand shake.
function shoot(meter, deg, radius, peakDegS, shake, seed) {
  const r = rng(seed);
  let t = 0;
  const hz = 60, dt = 1 / hz;
  const still = () => {
    for (let i = 0; i < hz * 0.5; i++) {
      t += dt * 1000;
      meter.sample({ alpha: gauss(r) * 2, beta: gauss(r) * 2, gamma: gauss(r) * 2 },
        { x: gauss(r) * shake, y: gauss(r) * shake, z: gauss(r) * shake }, t);
    }
  };
  still();
  // A smooth turn: speed rises and falls as a half sine.
  const dur = (deg / peakDegS) * Math.PI / 2;
  const steps = Math.round(dur * hz);
  for (let i = 0; i < steps; i++) {
    t += dt * 1000;
    const s = Math.sin(Math.PI * (i + 0.5) / steps);
    const wDeg = peakDegS * s;
    const w = wDeg * Math.PI / 180;
    const wDot = peakDegS * Math.PI / 180 * Math.PI / dur * Math.cos(Math.PI * (i + 0.5) / steps);
    meter.sample({ alpha: gauss(r) * 2, beta: gauss(r) * 2, gamma: wDeg + gauss(r) * 2 },
      { x: wDot * radius + gauss(r) * shake, y: gauss(r) * shake, z: -w * w * radius + gauss(r) * shake }, t);
  }
  still();
  return meter.result();
}

for (const [label, shake] of [['steady hands', 0.08], ['shaky hands', 0.25]]) {
  for (let seed = 1; seed <= 20; seed++) {
    const m = SwingMeter.create();
    const spot = shoot(m, 30, 0.03, 45, shake, seed);
    ok(`${label}: turning on the spot is not a swing (seed ${seed})`, !spot.swung,
       JSON.stringify(spot));
    m.reset();
    const arm = shoot(m, 30, 0.5, 45, shake, seed + 100);
    ok(`${label}: swinging at arm's length is a swing (seed ${seed})`, arm.swung,
       JSON.stringify(arm));
    ok(`${label}: the arm is measured near 0.5 m (seed ${seed})`,
       arm.radius > 0.3 && arm.radius < 0.75, arm.radius.toFixed(2));
  }
}

// Turned too slowly to read: say nothing rather than guess.
{
  const m = SwingMeter.create();
  const slow = shoot(m, 30, 0.5, 20, 0.08, 7);
  ok('a turn too slow to measure is not called a swing', !slow.swung, JSON.stringify(slow));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
