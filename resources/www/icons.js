// Inline SVG icons — font glyphs like ⏮⟳ render as empty boxes on some
// platforms (Linux Chrome), so actions use these instead. `ic(name)`
// returns an SVG string; buttons should still carry title/aria-label.
const IC_PATHS = {
  list: "M4 6h16M4 12h16M4 18h16",
  restart: "M20 11A8 8 0 1 0 19 16M20 5v6h-6",
  stop: "M7 7h10v10H7z",
  power: "M12 4v8M6.5 6.5a8 8 0 1 0 11 0",
  edit: "M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17v3zM13.5 6.5l3 3",
  x: "M6 6l12 12M18 6L6 18",
  prev: "M6 5v14M18 5l-9 7 9 7z",
  next: "M18 5v14M6 5l9 7-9 7z",
  pause: "M8 5v14M16 5v14",
  play: "M7 5l12 7-12 7z",
  hold: "M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7l8-4z",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 3",
  download: "M12 4v11m0 0l-4-4m4 4l4-4M5 19h14",
  upload: "M12 15V4m0 0L8 8m4-4l4 4M5 19h14",
};
function ic(name, size = 14) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `<path d="${IC_PATHS[name] || ""}"/></svg>`;
}
