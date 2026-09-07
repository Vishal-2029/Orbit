// The dialog behind "add a hotspot here".
//
// The viewer supplies the hard part - a click on the panorama comes back as a
// yaw and a pitch, because the engine's screenToCoordinates does the projection
// - so this is only a form. It handles both kinds, because they differ by two
// fields and splitting them would have meant two dialogs that had to stay in
// step with each other.
const HotspotEditor = (() => {
  "use strict";

  let openDialog = null;

  function close() {
    if (!openDialog) return;
    openDialog.remove();
    openDialog = null;
  }

  function field(label, hint) {
    const wrap = document.createElement("label");
    wrap.className = "he-field";
    const span = document.createElement("span");
    span.textContent = label;
    wrap.appendChild(span);
    if (hint) {
      const small = document.createElement("small");
      small.textContent = hint;
      wrap.appendChild(small);
    }
    return wrap;
  }

  /**
   * open({ hotspot, captures, currentCaptureId, onSave, onDelete })
   *
   * hotspot          the one being edited, or {kind, yaw, pitch} for a new one
   * captures         [{id, title}] that a direction hotspot may point at
   * currentCaptureId so a capture cannot be offered as a link to itself
   */
  function open(spec) {
    close();

    const hotspot = Object.assign({}, spec.hotspot);
    const isNew = !hotspot.id;

    const root = document.createElement("div");
    root.className = "he-backdrop";

    const card = document.createElement("div");
    card.className = "he-card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");

    const h2 = document.createElement("h2");
    h2.textContent = isNew ? "Add a hotspot" : "Edit hotspot";
    card.appendChild(h2);

    // --- kind ---------------------------------------------------------
    const kindRow = document.createElement("div");
    kindRow.className = "he-kinds";
    let kind = hotspot.kind || "info";

    function kindButton(value, label, description) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "he-kind" + (kind === value ? " active" : "");
      b.innerHTML =
        `<strong>${label}</strong><small>${description}</small>`;
      b.addEventListener("click", () => {
        kind = value;
        kindRow.querySelectorAll(".he-kind").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        applyKind();
      });
      return b;
    }
    // The kind is fixed once created: changing it turns the hotspot into a
    // different thing, and the server refuses it for the same reason.
    if (isNew) {
      kindRow.appendChild(kindButton("info", "Information",
        "A marker that opens a note"));
      kindRow.appendChild(kindButton("link", "Direction",
        "An arrow to another 360"));
      card.appendChild(kindRow);
    }

    // --- info fields --------------------------------------------------
    const titleField = field("Title");
    const titleInput = document.createElement("input");
    titleInput.type = "text";
    titleInput.maxLength = 120;
    titleInput.placeholder = "The fireplace";
    titleInput.value = hotspot.title || "";
    titleField.appendChild(titleInput);

    const bodyField = field("Text", "Shown when someone opens the marker");
    const bodyInput = document.createElement("textarea");
    bodyInput.rows = 4;
    bodyInput.maxLength = 2000;
    bodyInput.placeholder = "Original 1890s tiling, restored in 2019.";
    bodyInput.value = hotspot.body || "";
    bodyField.appendChild(bodyInput);

    // --- link fields --------------------------------------------------
    const targetField = field("Goes to");
    const targetSelect = document.createElement("select");
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Choose a 360…";
    targetSelect.appendChild(placeholder);
    (spec.captures || [])
      .filter((c) => c.id !== spec.currentCaptureId)
      .forEach((c) => {
        const o = document.createElement("option");
        o.value = c.id;
        o.textContent = c.title || "Untitled 360";
        if (c.id === hotspot.target_capture_id) o.selected = true;
        targetSelect.appendChild(o);
      });
    targetField.appendChild(targetSelect);

    const rotField = field("Arrow direction",
      "Turn the arrow so it points the way you would walk");
    const rotRow = document.createElement("div");
    rotRow.className = "he-rot";
    const rotInput = document.createElement("input");
    rotInput.type = "range";
    rotInput.min = "0";
    rotInput.max = "359";
    rotInput.step = "1";
    rotInput.value = String(Math.round(((hotspot.rotation || 0) * 180) / Math.PI));
    const rotPreview = document.createElement("div");
    rotPreview.className = "he-rot-preview";
    rotPreview.innerHTML =
      '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor"' +
      ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M12 19V5M5 12l7-7 7 7"/></svg>';
    function drawRot() {
      rotPreview.style.transform = `rotate(${rotInput.value}deg)`;
    }
    rotInput.addEventListener("input", drawRot);
    drawRot();
    rotRow.appendChild(rotInput);
    rotRow.appendChild(rotPreview);
    rotField.appendChild(rotRow);

    card.appendChild(titleField);
    card.appendChild(bodyField);
    card.appendChild(targetField);
    card.appendChild(rotField);

    function applyKind() {
      const info = kind === "info";
      titleField.style.display = "";
      bodyField.style.display = info ? "" : "none";
      targetField.style.display = info ? "none" : "";
      rotField.style.display = info ? "none" : "";
      titleField.querySelector("span").textContent = info ? "Title" : "Label";
      titleInput.placeholder = info ? "The fireplace" : "Leave empty to use the 360's name";
    }
    applyKind();

    // --- error line ---------------------------------------------------
    const error = document.createElement("div");
    error.className = "he-error";
    error.hidden = true;
    card.appendChild(error);

    function showError(msg) {
      error.textContent = msg;
      error.hidden = false;
    }

    // --- buttons ------------------------------------------------------
    const actions = document.createElement("div");
    actions.className = "he-actions";

    if (!isNew && spec.onDelete) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "he-delete";
      del.textContent = "Delete";
      del.addEventListener("click", async () => {
        del.disabled = true;
        try {
          await spec.onDelete(hotspot);
          close();
        } catch (e) {
          del.disabled = false;
          showError(e.message || "Could not delete that hotspot.");
        }
      });
      actions.appendChild(del);
    }

    const spacer = document.createElement("div");
    spacer.style.flex = "1";
    actions.appendChild(spacer);

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", close);
    actions.appendChild(cancel);

    const save = document.createElement("button");
    save.type = "button";
    save.className = "primary";
    save.textContent = isNew ? "Add" : "Save";
    actions.appendChild(save);
    card.appendChild(actions);

    save.addEventListener("click", async () => {
      const payload = {
        kind: kind,
        yaw: hotspot.yaw,
        pitch: hotspot.pitch,
        title: titleInput.value.trim(),
        body: kind === "info" ? bodyInput.value.trim() : "",
      };
      if (kind === "link") {
        payload.target_capture_id = targetSelect.value;
        payload.rotation = (Number(rotInput.value) * Math.PI) / 180;
        if (!payload.target_capture_id) {
          showError("Choose which 360 this arrow leads to.");
          return;
        }
        // The arrow's tooltip falls back to the target's own name, so a label
        // is genuinely optional here - but the server wants a title on an info
        // hotspot, and an empty one on a link is fine.
      } else if (!payload.title) {
        showError("Give this marker a title.");
        return;
      }

      save.disabled = true;
      save.textContent = "Saving…";
      try {
        await spec.onSave(payload, hotspot.id || null);
        close();
      } catch (e) {
        save.disabled = false;
        save.textContent = isNew ? "Add" : "Save";
        showError(e.message || "Could not save that hotspot.");
      }
    });

    // Clicking the backdrop cancels; clicking the card must not.
    root.addEventListener("click", (ev) => {
      if (ev.target === root) close();
    });
    card.addEventListener("click", (ev) => ev.stopPropagation());

    function onKey(ev) {
      if (ev.key === "Escape") close();
    }
    document.addEventListener("keydown", onKey, { once: true });

    root.appendChild(card);
    document.body.appendChild(root);
    openDialog = root;
    titleInput.focus();
  }

  return { open, close };
})();
