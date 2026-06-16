#!/usr/bin/env bash
# Render a README locally the way GitHub does — mermaid diagrams, images,
# light/dark, and GitHub's own stylesheet.
#
#   ./preview-readme.sh [path/to/repo]
#
# Writes README.preview.html next to the README (so relative image paths
# resolve) and opens it. Delete the file when you're done; it is a throwaway.

set -euo pipefail

REPO="${1:-.}"
REPO="$(cd "$REPO" && pwd)"
README="$REPO/README.md"
OUT="$REPO/README.preview.html"

[ -f "$README" ] || { echo "No README.md in $REPO" >&2; exit 1; }

python3 - "$README" "$OUT" <<'PY'
import html, json, sys, pathlib

src, out = sys.argv[1], sys.argv[2]
markdown = pathlib.Path(src).read_text(encoding="utf-8")

page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>README preview</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.8.1/github-markdown.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/github.min.css">
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; background: #ffffff; }
  @media (prefers-color-scheme: dark) { body { background: #0d1117; } }
  .wrap { max-width: 1012px; margin: 0 auto; padding: 32px 16px 96px; }
  .markdown-body { background: transparent; }
  .markdown-body img { border-radius: 6px; border: 1px solid rgba(128,128,128,.25); }
  .bar {
    position: sticky; top: 0; z-index: 5;
    display: flex; gap: 12px; align-items: center; justify-content: space-between;
    padding: 10px 16px; margin-bottom: 24px;
    font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
    border-bottom: 1px solid rgba(128,128,128,.25);
    background: rgba(127,127,127,.06); backdrop-filter: blur(8px);
  }
  .bar button {
    font: inherit; cursor: pointer; padding: 4px 10px; border-radius: 5px;
    border: 1px solid rgba(128,128,128,.35); background: transparent; color: inherit;
  }
  pre.mermaid { background: transparent; text-align: center; }
</style>
</head>
<body>
<div class="bar">
  <span id="src"></span>
  <span>
    <button onclick="location.reload()">Reload</button>
    <button id="theme">Toggle theme</button>
  </span>
</div>
<div class="wrap"><article class="markdown-body" id="content">Rendering…</article></div>

<script type="module">
  import markdownit from "https://cdn.jsdelivr.net/npm/markdown-it@14.1.0/+esm";
  import hljs from "https://cdn.jsdelivr.net/npm/highlight.js@11.11.1/+esm";
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";

  const source = __MARKDOWN__;
  document.getElementById("src").textContent = __SRCPATH__;

  const md = markdownit({
    html: true,
    linkify: true,
    highlight(code, lang) {
      // Leave mermaid blocks alone; mermaid renders them itself.
      if (lang === "mermaid") return null;
      if (lang && hljs.getLanguage(lang)) {
        try { return hljs.highlight(code, { language: lang }).value; } catch {}
      }
      return "";
    },
  });

  // Route ```mermaid fences to <pre class="mermaid">, which is what GitHub does.
  const fence = md.renderer.rules.fence;
  md.renderer.rules.fence = (tokens, idx, opts, env, self) => {
    const token = tokens[idx];
    if ((token.info || "").trim() === "mermaid") {
      return `<pre class="mermaid">${token.content
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")}</pre>`;
    }
    return fence(tokens, idx, opts, env, self);
  };

  document.getElementById("content").innerHTML = md.render(source);
  document.querySelectorAll("pre code").forEach((el) => el.classList.add("hljs"));

  const dark = () =>
    document.documentElement.dataset.theme === "dark" ||
    (!document.documentElement.dataset.theme &&
      matchMedia("(prefers-color-scheme: dark)").matches);

  const draw = () => {
    mermaid.initialize({ startOnLoad: false, theme: dark() ? "dark" : "default" });
    document.querySelectorAll("pre.mermaid").forEach((el) => {
      if (el.dataset.src) el.textContent = el.dataset.src;
      else el.dataset.src = el.textContent;
      el.removeAttribute("data-processed");
    });
    mermaid.run({ querySelector: "pre.mermaid" });
  };
  draw();

  document.getElementById("theme").onclick = () => {
    const root = document.documentElement;
    root.dataset.theme = dark() ? "light" : "dark";
    root.style.colorScheme = root.dataset.theme;
    document.body.style.background = dark() ? "#0d1117" : "#ffffff";
    draw();
  };
</script>
</body>
</html>
"""

page = page.replace("__MARKDOWN__", json.dumps(markdown))
page = page.replace("__SRCPATH__", json.dumps(src))
pathlib.Path(out).write_text(page, encoding="utf-8")
print(out)
PY

echo "Opening $OUT"
open "$OUT"
