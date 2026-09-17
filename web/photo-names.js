// What a photo is called, and which way it faced - in one place.
//
// The restitch screen and the 360 viewer both name photos, and they have to
// agree: "the join between Photo 3 and Photo 4 is wrong" in the viewer is only
// useful if Photo 3 on the restitch screen is the same photo. So the number is
// the photo's position among ALL of a capture's photos in shot order -
// excluded ones included, so removing a photo never renumbers the rest.
const PhotoNames = (() => {
  // Where the camera was pointing, in words rather than numbers alone. The
  // capture flow talks to people in compass terms ("turn a quarter turn to your
  // RIGHT"), so everything that reports on a capture should read the same way.
  function facing(yaw) {
    const y = ((Number(yaw) || 0) % 360 + 360) % 360;
    const names = ["ahead", "ahead-right", "right", "behind-right",
                   "behind", "behind-left", "left", "ahead-left"];
    return names[Math.round(y / 45) % 8];
  }

  function tilt(pitch) {
    const p = Math.round(Number(pitch) || 0);
    if (p >= 60) return ", up";
    if (p >= 30) return ", tilted up";
    if (p <= -60) return ", down";
    if (p <= -30) return ", tilted down";
    return "";
  }

  /** "ahead-right, tilted down · 45°, -45°" */
  function position(f) {
    const y = Math.round(Number(f.yaw) || 0);
    const p = Math.round(Number(f.pitch) || 0);
    return `${facing(f.yaw)}${tilt(f.pitch)} · ${y}°, ${p >= 0 ? "+" : ""}${p}°`;
  }

  /** Map of frame index -> photo number, from the capture's full frame list. */
  function numbers(frames) {
    const map = {};
    (frames || []).slice().sort((a, b) => a.index - b.index)
      .forEach((f, i) => { map[f.index] = i + 1; });
    return map;
  }

  return { facing, tilt, position, numbers };
})();
