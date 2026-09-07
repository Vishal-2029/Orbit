// The markup for a hotspot.
//
// The panorama engine handles WHERE a hotspot goes - it repositions whatever
// element we give it every frame and hides it when it swings behind the camera.
// This file is only concerned with WHAT that element is.
//
// Two kinds:
//   info  - a dot that opens a title and a paragraph
//   link  - an arrow on the floor that walks you to another capture
//
// Adapted from the engine's own sample tour, with three changes. The icons are
// inline SVG rather than PNG files, so there are no image assets to ship and 
// they stay sharp at any size. The mobile panel is the SAME element restyled by
// a media query, where the sample clones its innerHTML into a second element on
// document.body - that clone loses its event handlers, has to be re-wired by
// re-querying, and is never removed when the hotspot goes away. And an edit and
// delete control appear when the viewer is in editing mode.
const Hotspots = (() => {
  "use strict";

  // Touch and pointer events must not reach the panorama underneath, or the
  // engine's drag-to-look takes over and the hotspot never gets its click.
  const SWALLOW = [
    "touchstart", "touchmove", "touchend", "touchcancel",
    "pointerdown", "pointermove", "pointerup", "pointercancel",
    "mousedown", "mousemove", "mouseup", "wheel",
  ];

  function stopPropagation(el) {
    SWALLOW.forEach((name) =>
      el.addEventListener(name, (ev) => ev.stopPropagation()));
  }

  function svg(paths, size) {
    const s = size || 24;
    return `<svg viewBox="0 0 24 24" width="${s}" height="${s}" aria-hidden="true"
                 fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
  }

  const ICON_INFO = svg('<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>');
  const ICON_ARROW = svg('<path d="M12 19V5M5 12l7-7 7 7"/>', 26);
  const ICON_CLOSE = svg('<path d="M18 6 6 18M6 6l12 12"/>', 18);
  const ICON_PENCIL = svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>', 15);
  const ICON_TRASH = svg('<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/>', 15);

  function editControls(hotspot, opts) {
    if (!opts.editable) return null;
    const bar = document.createElement("div");
    bar.className = "hotspot-edit";

    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "hotspot-edit-btn";
    edit.title = "Edit this hotspot";
    edit.setAttribute("aria-label", "Edit this hotspot");
    edit.innerHTML = ICON_PENCIL;
    edit.addEventListener("click", (ev) => {
      ev.stopPropagation();
      opts.onEdit && opts.onEdit(hotspot);
    });

    const del = document.createElement("button");
    del.type = "button";
    del.className = "hotspot-edit-btn danger";
    del.title = "Delete this hotspot";
    del.setAttribute("aria-label", "Delete this hotspot");
    del.innerHTML = ICON_TRASH;
    del.addEventListener("click", (ev) => {
      ev.stopPropagation();
      opts.onDelete && opts.onDelete(hotspot);
    });

    bar.appendChild(edit);
    bar.appendChild(del);
    return bar;
  }

  function createLink(hotspot, opts) {
    const wrapper = document.createElement("div");
    wrapper.className = "hotspot link-hotspot";

    const icon = document.createElement("div");
    icon.className = "link-hotspot-icon";
    icon.innerHTML = ICON_ARROW;
    // rotation points the arrow along the floor towards where you are going.
    icon.style.transform = `rotate(${hotspot.rotation || 0}rad)`;

    const tooltip = document.createElement("div");
    tooltip.className = "hotspot-tooltip link-hotspot-tooltip";
    tooltip.textContent = hotspot.target_title || "Go here";

    wrapper.appendChild(icon);
    wrapper.appendChild(tooltip);

    const controls = editControls(hotspot, opts);
    if (controls) wrapper.appendChild(controls);

    wrapper.addEventListener("click", (ev) => {
      ev.stopPropagation();
      // In editing mode a click is for selecting, not for travelling - being
      // teleported away every time you try to adjust a marker is maddening.
      if (opts.editable) {
        opts.onEdit && opts.onEdit(hotspot);
        return;
      }
      opts.onActivate && opts.onActivate();
    });
    stopPropagation(wrapper);
    return wrapper;
  }

  function createInfo(hotspot, opts) {
    const wrapper = document.createElement("div");
    wrapper.className = "hotspot info-hotspot";

    const header = document.createElement("button");
    header.type = "button";
    header.className = "info-hotspot-header";
    header.setAttribute("aria-expanded", "false");

    const iconWrap = document.createElement("span");
    iconWrap.className = "info-hotspot-icon-wrapper";
    iconWrap.innerHTML = ICON_INFO;

    const titleWrap = document.createElement("span");
    titleWrap.className = "info-hotspot-title-wrapper";
    const title = document.createElement("span");
    title.className = "info-hotspot-title";
    title.textContent = hotspot.title || "Info";
    titleWrap.appendChild(title);

    header.appendChild(iconWrap);
    header.appendChild(titleWrap);

    const panel = document.createElement("div");
    panel.className = "info-hotspot-panel";

    const panelHead = document.createElement("div");
    panelHead.className = "info-hotspot-panel-head";
    const panelTitle = document.createElement("h3");
    panelTitle.textContent = hotspot.title || "Info";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "info-hotspot-close";
    close.title = "Close";
    close.setAttribute("aria-label", "Close");
    close.innerHTML = ICON_CLOSE;
    panelHead.appendChild(panelTitle);
    panelHead.appendChild(close);

    const body = document.createElement("div");
    body.className = "info-hotspot-text";
    // textContent, never innerHTML: this string came from whoever authored the
    // capture, and it is shown to everyone who opens the share link.
    body.textContent = hotspot.body || "";

    panel.appendChild(panelHead);
    panel.appendChild(body);

    wrapper.appendChild(header);
    wrapper.appendChild(panel);

    const controls = editControls(hotspot, opts);
    if (controls) wrapper.appendChild(controls);

    function setOpen(open) {
      wrapper.classList.toggle("open", open);
      header.setAttribute("aria-expanded", open ? "true" : "false");
    }
    header.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setOpen(!wrapper.classList.contains("open"));
      opts.onActivate && opts.onActivate();
    });
    close.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setOpen(false);
    });

    stopPropagation(wrapper);
    return wrapper;
  }

  function createElement(hotspot, opts) {
    const o = opts || {};
    return hotspot.kind === "link" ? createLink(hotspot, o) : createInfo(hotspot, o);
  }

  return { createElement };
})();
