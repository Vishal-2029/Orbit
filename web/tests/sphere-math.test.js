// Projection maths for the blue-dot capture guide.
// Run with:  node web/tests/sphere-math.test.js
const O = require('../orientation.js');
global.Orientation = O;
const S = require('../sphere-math.js');
const DEG=Math.PI/180;
let pass=0, fail=0;
function ok(name, cond, extra=''){ if(cond){pass++;} else {fail++; console.log('  FAIL:',name,extra);} }

// quaternion for rotating about world Z (yaw) by deg, starting from identity-ish
// Build device rotation: device at identity looks along -Z(device). We want a
// quaternion that yaws the device about world up (+Z).
function yawQuat(deg){ const h=deg*DEG/2; return [0,0,Math.sin(h),Math.cos(h)]; }
function pitchQuatX(deg){ const h=deg*DEG/2; return [Math.sin(h),0,0,Math.cos(h)]; }

// identity: forward = (0,0,-1). That's straight DOWN in world (since world up=+Z).
// So to have a sane horizontal reference, tilt device up 90 about X first.
const upright = pitchQuatX(90);            // now forward ~ (0,1,0) = horizontal
const f0 = S.forwardOf(upright);
ok('upright forward is horizontal', Math.abs(f0[2])<1e-6, JSON.stringify(f0.map(n=>+n.toFixed(3))));

const frame = S.referenceFrame(upright);
ok('frame.forward horizontal', Math.abs(frame.forward[2])<1e-6);
ok('frame.left perpendicular', Math.abs(S.dot(frame.forward,frame.left))<1e-6);

// heading at baseline should be yaw 0
let hd = S.headingOf(frame, upright);
ok('baseline yaw==0', Math.abs(hd.yaw)<0.01 || Math.abs(hd.yaw-360)<0.01, 'yaw='+hd.yaw.toFixed(2));
ok('baseline pitch==0', Math.abs(hd.pitch)<0.01, 'pitch='+hd.pitch.toFixed(2));

// Turn the device RIGHT by 90deg about world up. Right = clockwise seen from above = negative Z rotation.
const turnedRight = O.qMultiply(yawQuat(-90), upright);
hd = S.headingOf(frame, turnedRight);
ok('turn right 90 -> yaw 90', Math.abs(hd.yaw-90)<0.5, 'yaw='+hd.yaw.toFixed(2));

const turnedRight180 = O.qMultiply(yawQuat(-180), upright);
ok('turn right 180 -> yaw 180', Math.abs(S.headingOf(frame,turnedRight180).yaw-180)<0.5,
   'yaw='+S.headingOf(frame,turnedRight180).yaw.toFixed(2));

const turnedLeft90 = O.qMultiply(yawQuat(90), upright);
ok('turn left 90 -> yaw 270', Math.abs(S.headingOf(frame,turnedLeft90).yaw-270)<0.5,
   'yaw='+S.headingOf(frame,turnedLeft90).yaw.toFixed(2));

// --- projection ---
const view={width:1000,height:1500,hfov:65};
// Front target while facing front => dot dead centre
const dFront = S.directionFor(frame,0,0);
let p = S.project(upright, dFront, view);
ok('front dot centred x', Math.abs(p.x-500)<0.5, 'x='+p.x.toFixed(2));
ok('front dot centred y', Math.abs(p.y-750)<0.5, 'y='+p.y.toFixed(2));
ok('front dot angle 0', p.angle<0.01, 'angle='+p.angle.toFixed(3));
ok('front dot visible', p.visible===true);

// Right target (yaw 90) while facing front => behind/off to the side
const dRight = S.directionFor(frame,90,0);
p = S.project(upright, dRight, view);
ok('right target is 90deg off axis', Math.abs(p.angle-90)<0.5, 'angle='+p.angle.toFixed(2));
ok('right target not visible when facing front', p.visible===false);

// after turning right 90, the right target should be centred
p = S.project(turnedRight, dRight, view);
ok('after turning right, right dot centred', Math.abs(p.x-500)<1 && Math.abs(p.y-750)<1,
   `x=${p.x.toFixed(1)} y=${p.y.toFixed(1)}`);
ok('after turning right, angle 0', p.angle<0.5, 'angle='+p.angle.toFixed(2));

// A target 20deg to the right should appear RIGHT of centre (x>centre)
const d20 = S.directionFor(frame,20,0);
p = S.project(upright, d20, view);
ok('20deg right appears right of centre', p.x>500, 'x='+p.x.toFixed(1));
ok('20deg right stays on screen', p.x<1000, 'x='+p.x.toFixed(1));
ok('20deg right angle==20', Math.abs(p.angle-20)<0.5, 'angle='+p.angle.toFixed(2));

// A target 20deg to the LEFT appears left of centre
const dL20 = S.directionFor(frame,340,0);
p = S.project(upright, dL20, view);
ok('20deg left appears left of centre', p.x<500, 'x='+p.x.toFixed(1));

// Pitch up target appears ABOVE centre (smaller y)
const dUp = S.directionFor(frame,0,20);
p = S.project(upright, dUp, view);
ok('pitch-up dot is above centre', p.y<750, 'y='+p.y.toFixed(1));
ok('pitch-up angle==20', Math.abs(p.angle-20)<0.5, 'angle='+p.angle.toFixed(2));

// Straight up target
const dZenith = S.directionFor(frame,0,90);
p = S.project(upright, dZenith, view);
ok('zenith is 90deg off when level', Math.abs(p.angle-90)<0.5, 'angle='+p.angle.toFixed(2));

// focal length sanity: at hfov 65, half-width maps to 32.5deg
const f = S.focalLength(1000,65);
const halfAngle = Math.atan((500)/f)/DEG;
ok('edge of frame == half fov', Math.abs(halfAngle-32.5)<0.01, halfAngle.toFixed(3));

console.log(`\n  ${pass} passed, ${fail} failed`);
process.exit(fail?1:0);
