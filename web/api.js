// Thin wrapper around the Orbit REST + WS API.
const OrbitAPI = (() => {
  const base = () => window.ORBIT_API_BASE;

  async function req(method, path, body, isForm) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      if (isForm) {
        opts.body = body; // FormData sets its own content-type
      } else {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
      }
    }
    const res = await fetch(base() + path, opts);
    if (!res.ok) {
      let msg = res.statusText;
      let body = null;
      try {
        body = await res.json();
        if (body && body.error) msg = body.error;
      } catch (_) {}
      const err = new Error(msg || `HTTP ${res.status}`);
      err.status = res.status;
      // Structured fields (e.g. code:"duplicate_direction") so callers can
      // react to a specific problem instead of string-matching the message.
      err.code = body && body.code;
      err.body = body;
      throw err;
    }
    if (res.status === 204) return null;
    return res.json();
  }

  return {
    createCapture(title, mode, opts) {
      const body = { title, mode };
      // Only sent when the caller has an opinion. The field is a *bool on the
      // server, so omitting it leaves the mode's own default in place.
      if (opts && typeof opts.includeUpDown === "boolean") {
        body.include_up_down = opts.includeUpDown;
      }
      return req("POST", "/api/v1/captures", body);
    },
    listCaptures(limit = 50, offset = 0) {
      return req("GET", `/api/v1/captures?limit=${limit}&offset=${offset}`);
    },
    getCapture(id) {
      return req("GET", `/api/v1/captures/${id}`);
    },
    getPlan(id) {
      return req("GET", `/api/v1/captures/${id}/plan`);
    },
    patchCapture(id, patch) {
      return req("PATCH", `/api/v1/captures/${id}`, patch);
    },
    deleteCapture(id) {
      return req("DELETE", `/api/v1/captures/${id}`);
    },
    uploadPhoto(id, { blob, index, slotId, yaw, pitch, hasHeading, quat, source, filename }) {
      const fd = new FormData();
      fd.append("photo", blob, filename || `${index}.jpg`);
      fd.append("index", String(index));
      fd.append("slot_id", slotId || "");
      fd.append("yaw", String(yaw ?? 0));
      fd.append("pitch", String(pitch ?? 0));
      // Tells the server whether yaw/pitch are real compass readings. Without
      // it the server cannot check that this photo faces a new direction.
      fd.append("has_heading", hasHeading ? "true" : "false");
      // Full 3D rotation of the phone at the moment of the shot. Yaw alone
      // cannot describe a tilted camera; the quaternion can, and it is what a
      // pose-aware stitcher needs.
      if (quat && quat.length === 4) {
        fd.append("qx", String(quat[0]));
        fd.append("qy", String(quat[1]));
        fd.append("qz", String(quat[2]));
        fd.append("qw", String(quat[3]));
      }
      if (source) fd.append("orientation_source", source);
      return req("POST", `/api/v1/captures/${id}/photos`, fd, true);
    },
    process(id) {
      return req("POST", `/api/v1/captures/${id}/process`);
    },
    // Build ONE ring, while the rest of the capture is still being shot.
    // Returns that ring's row, not the capture: the 360 does not exist yet.
    processRing(id, ring) {
      return req("POST", `/api/v1/captures/${id}/process`, { ring });
    },
    listRings(id) {
      return req("GET", `/api/v1/captures/${id}/rings`);
    },
    listFrames(id) {
      return req("GET", `/api/v1/captures/${id}/frames`);
    },
    // Hold one photo back from the next build, or put it back. Nothing is
    // deleted. Returns the whole frame list as the server now holds it, so the
    // caller redraws from that rather than from what it assumed its click did.
    setFrameExcluded(id, index, excluded) {
      return req("PATCH", `/api/v1/captures/${id}/frames/${index}`, { excluded });
    },
    // Where a person put one photo, and whether it is locked there. Radians in
    // the viewer's frame, the same as hotspots and manifest.photos.
    setFramePose(id, index, { yaw, pitch, roll, locked }) {
      return req("PATCH", `/api/v1/captures/${id}/frames/${index}`, {
        manual_yaw: yaw, manual_pitch: pitch, manual_roll: roll || 0,
        manual_locked: !!locked,
      });
    },
    getManifest(id) {
      return req("GET", `/api/v1/captures/${id}/manifest`);
    },
    manifestBySlug(slug) {
      return req("GET", `/s/${slug}/manifest`);
    },

    listHotspots(captureId) {
      return req("GET", `/api/v1/captures/${captureId}/hotspots`);
    },
    createHotspot(captureId, hotspot) {
      return req("POST", `/api/v1/captures/${captureId}/hotspots`, hotspot);
    },
    updateHotspot(hotspotId, patch) {
      return req("PATCH", `/api/v1/hotspots/${hotspotId}`, patch);
    },
    deleteHotspot(hotspotId) {
      return req("DELETE", `/api/v1/hotspots/${hotspotId}`);
    },
    // version stamps the URL so a rebuilt picture is fetched rather than served
    // from cache. These images are replaced in place at a fixed URL, and they
    // are sent with a year's immutable caching, so without it a rebuild shows
    // the old photo forever. Pass the capture's updated_at.
    imageURL(id, kind, idx, version) {
      const v = version ? `?v=${encodeURIComponent(version)}` : "";
      return `${base()}/api/v1/captures/${id}/image/${kind}/${idx}${v}`;
    },
    // Seconds, to match the stamp the server puts in manifest URLs.
    cacheStamp(capture) {
      const t = Date.parse(capture && capture.updated_at);
      return Number.isFinite(t) ? Math.floor(t / 1000) : "";
    },
    panoramaURL(id) {
      return `${base()}/api/v1/captures/${id}/image/panorama`;
    },
    connectWS(id, handlers) {
      const url = `${window.ORBIT_WS_BASE}/ws/captures/${id}`;
      const ws = new WebSocket(url);
      ws.onopen = () => handlers.onOpen && handlers.onOpen();
      ws.onmessage = (ev) => {
        let data;
        try {
          data = JSON.parse(ev.data);
        } catch (e) {
          return;
        }
        handlers.onEvent && handlers.onEvent(data);
      };
      ws.onerror = (e) => handlers.onError && handlers.onError(e);
      ws.onclose = () => handlers.onClose && handlers.onClose();
      return ws;
    },
  };
})();
