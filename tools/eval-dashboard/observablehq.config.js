// Observable Framework configuration.
// Docs: https://observablehq.com/framework/config
export default {
  title: "Eval Harness Dashboard",
  // `air`/`near-midnight` = the built-in auto light/dark theme pair; Plot marks
  // and text adapt to the viewer's colour scheme.
  theme: ["air", "near-midnight"],
  root: "src",
  // Static-export friendly: no header/footer chrome, single page.
  pager: false,
  toc: false,
  // Search across the (single) page is unnecessary for a one-pager.
  search: false,
  // If you deploy under a GitHub Pages *project* path
  // (https://<user>.github.io/<repo>/…), set the base path here and rebuild,
  // e.g. base: "/avatar-evals-ecommerce-portal/tools/eval-dashboard/".
  // Left as "/" for local `open dist/index.html` + user-site Pages.
  cleanUrls: true,
};
