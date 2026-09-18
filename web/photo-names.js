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

  // Which ring a photo belongs to, from the slot the capture flow planned it
  // for. A sphere capture is shot as rings - level all the way round, then
  // tilted up, then tilted down, then the ceiling and floor - and that is how
  // people think about it, so it is how the screens group it.
  //
  // `order` is shooting order, not height: the level ring is shot first.
  const RINGS = [
    { id: "r+0", label: "Horizontal ring", hint: "level, all the way round", order: 1 },
    { id: "r+45", label: "Top ring", hint: "tilted up", order: 2 },
    { id: "r-45", label: "Bottom ring", hint: "tilted down", order: 3 },
    { id: "up", label: "Ceiling", hint: "straight up", order: 4 },
    { id: "down", label: "Floor", hint: "straight down", order: 5 },
  ];

  /** The ring a frame belongs to: { id, label, hint, order }. */
  function ring(f) {
    const slot = String((f && f.slot_id) || "");
    const id = slot.replace(/_\d+$/, "");
    const known = RINGS.find((r) => r.id === id);
    if (known) return known;
    // A capture shot at some other tilt, or with no slot at all. Named from
    // the angle rather than dropped, so nothing goes missing from the screen.
    const m = id.match(/^r([+-])(\d+)$/);
    if (m) {
      const deg = Number(m[2]);
      return { id, order: 6,
               label: deg === 0 ? "Horizontal ring" : `${m[1] === "+" ? "Top" : "Bottom"} ring`,
               hint: deg === 0 ? "level" : `tilted ${m[1] === "+" ? "up" : "down"} ${deg}\u00b0` };
    }
    return { id: id || "other", label: "Other photos", hint: "", order: 7 };
  }

  /** A capture's frames as [{ ring, frames }], in shooting order. */
  function byRing(frames) {
    const groups = new Map();
    (frames || []).forEach((f) => {
      const r = ring(f);
      if (!groups.has(r.id)) groups.set(r.id, { ring: r, frames: [] });
      groups.get(r.id).frames.push(f);
    });
    return Array.from(groups.values())
      .sort((a, b) => a.ring.order - b.ring.order || a.ring.id.localeCompare(b.ring.id));
  }

  /** Map of frame index -> photo number, from the capture's full frame list. */
  function numbers(frames) {
    const map = {};
    (frames || []).slice().sort((a, b) => a.index - b.index)
      .forEach((f, i) => { map[f.index] = i + 1; });
    return map;
  }

  return { facing, tilt, position, numbers, ring, byRing, RINGS };
})();
