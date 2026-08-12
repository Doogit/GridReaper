/* Tailwind content config for the GridSignals web UI (R8.9 UI port).
 *
 * Loaded via `@config "./tailwind.config.js"` from tailwind.input.css. In
 * Tailwind v4 the theme lives in the CSS @theme block; this file only tells the
 * standalone CLI which server-rendered files to scan for utility-class usage.
 * Globs are relative to this file (app/ui_web/). render.py joins the list once
 * it carries class-bearing view helpers (Chunk 1+).
 */
module.exports = {
  content: ["./templates/**/*.html"],
};
