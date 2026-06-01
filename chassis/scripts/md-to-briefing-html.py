#!/opt/homebrew/bin/python3
"""md-to-briefing-html.py — wrap a briefing markdown file as a self-contained
newspaper-style HTML with a sticky left nav and serif typography.

Usage:
    python3 scripts/md-to-briefing-html.py <input.md> [--output <output.html>] [--title "..."]

Design goals:
- Single file output (no external CSS, no network fonts, no JS deps)
- Left sticky sidebar auto-generated from top-level sections (H1 + H2)
- Dark-mode aware via prefers-color-scheme
- Mobile collapses sidebar to a top-drawer toggle
- Callout styling for sections containing attention triggers like "Asks for the installer"
- No em dashes (per CLAUDE.md Copy Quality rule) - the content rule, not a formatter concern

Sections the reader might flip between:
- Daily Stoic
- Claude Code Pulse
- Events
- Community Groups Digest
- Asks / Waiting on installer
- Editorial / Content
- Spend

This script does NOT edit the source content. Bad markdown in = bad HTML out.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

try:
    import markdown
except ImportError:
    print("ERROR: python-markdown not installed. Run: pip3 install --user --break-system-packages markdown", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Callout detection
# ---------------------------------------------------------------------------

CALLOUT_TRIGGERS = {
    "attention": (
        r"\b(ask|asks|waiting on|needs? sean|needs? your|action needed|awaiting|required|decisions?)\b",
        "Sections that need the installer's attention — decisions, approvals, follow-ups.",
    ),
    "warning": (
        r"\b(broke|break|failed|failure|error|incident|gap|stale|missed|warning)\b",
        "Problems or gaps surfaced in this briefing.",
    ),
}


def classify_heading(text: str) -> str | None:
    t = text.lower()
    for name, (pattern, _) in CALLOUT_TRIGGERS.items():
        if re.search(pattern, t):
            return name
    return None


# ---------------------------------------------------------------------------
# TOC extraction
# ---------------------------------------------------------------------------

HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


def slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug or "section"


def extract_sections(md_src: str) -> list[tuple[int, str, str, str | None]]:
    """Return list of (level, text, slug, callout_class)."""
    md_src = preprocess(md_src)
    sections: list[tuple[int, str, str, str | None]] = []
    seen_slugs: set[str] = set()
    for match in HEADING_RE.finditer(md_src):
        level = len(match.group(1))
        text = match.group(2).strip()
        slug = slugify(text)
        base_slug = slug
        idx = 1
        while slug in seen_slugs:
            idx += 1
            slug = f"{base_slug}-{idx}"
        seen_slugs.add(slug)
        callout = classify_heading(text)
        sections.append((level, text, slug, callout))
    return sections


def build_toc(sections: list[tuple[int, str, str, str | None]]) -> str:
    if not sections:
        return ""
    items = []
    for level, text, slug, callout in sections:
        # Only show H1 + H2 in the sidebar to avoid clutter
        if level > 2:
            continue
        css = f"toc-level-{level}"
        if callout:
            css += f" toc-callout toc-callout-{callout}"
        safe_text = html.escape(text)
        items.append(
            f'<li class="{css}"><a href="#{slug}">'
            + (f'<span class="toc-callout-icon">{"🔔" if callout == "attention" else "⚠️"}</span> ' if callout else "")
            + f'<span class="toc-text">{safe_text}</span></a></li>'
        )
    return '<ul class="toc-list">' + "\n".join(items) + "</ul>"


# ---------------------------------------------------------------------------
# Markdown -> HTML body
# ---------------------------------------------------------------------------

ISSUE_REF_RE = re.compile(r"(^|[\s(\[])#(\d+)\b")


def preprocess(md_src: str) -> str:
    """Escape GitHub-style issue refs (`#186`) so markdown doesn't occasionally
    treat them as H1s inside list items or at line starts. `\\#186` renders
    as literal `#186`, which is what we want. Also strip the first H1 since
    the masthead already shows the title - no need to duplicate it in the body."""
    escaped = ISSUE_REF_RE.sub(lambda m: f"{m.group(1)}\\#{m.group(2)}", md_src)
    # Drop the first H1 (and optionally the blank line right after)
    escaped = re.sub(r"\A\s*#\s+[^\n]+\n+", "", escaped, count=1)
    return escaped


_EXTERNAL_ANCHOR_RE = re.compile(
    r'(<a\b(?![^>]*\btarget=)[^>]*\bhref=")(https?://[^"]+)(")',
    flags=re.IGNORECASE,
)


def _add_target_blank(body_html: str) -> str:
    """Open external links in a new tab. Critical when the briefing is viewed
    inside the dashboard iframe (`/briefings` wraps `/briefings/files/...` in
    an <iframe>): without target=_blank, clicks try to navigate the iframe,
    and sites with X-Frame-Options: DENY (Anthropic, HN, GitHub, OpenAI, ...)
    silently refuse to load. Result the V1 reference install hit: "none of the links
    resolve." Skip in-page `#foo` anchors — they still belong in-frame.
    """
    return _EXTERNAL_ANCHOR_RE.sub(
        lambda m: f'{m.group(1)}{m.group(2)}{m.group(3)} target="_blank" rel="noopener noreferrer"',
        body_html,
    )


def render_body(md_src: str) -> str:
    md_src = preprocess(md_src)
    md = markdown.Markdown(
        extensions=[
            "tables",
            "fenced_code",
            "attr_list",
            "sane_lists",
            "toc",  # adds id="..." to headings
        ],
        extension_configs={
            "toc": {"permalink": False, "title": ""},
        },
    )
    body = md.convert(md_src)
    # Wrap attention / warning H2 sections in callout containers for accent styling
    body = wrap_section_callouts(body)
    body = _add_target_blank(body)
    return body


def wrap_section_callouts(body_html: str) -> str:
    """Add a `data-callout="attention|warning"` attribute + icon prefix to H2s
    whose text matches our triggers, so CSS can give them an accent rule."""
    def replace(match: re.Match) -> str:
        attrs = match.group(1) or ""
        inner = match.group(2)
        callout = classify_heading(inner)
        if not callout:
            return match.group(0)
        icon = "🔔" if callout == "attention" else "⚠️"
        # Inject data-callout attr
        if "data-callout=" not in attrs:
            attrs = (attrs + f' data-callout="{callout}"').strip()
        return f'<h2 {attrs}><span class="h-icon">{icon}</span> {inner}</h2>'
    return re.sub(r"<h2([^>]*)>(.*?)</h2>", replace, body_html, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #fbf9f4;
  --fg: #1a1a1a;
  --muted: #666;
  --rule: #d9d4c7;
  --accent: #8a3a2c;
  --attention: #b8762a;
  --warning: #9c3b35;
  --code-bg: #efeadd;
  --serif: 'Iowan Old Style', 'Charter', 'Georgia', 'Times New Roman', serif;
  --sans: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif;
  --mono: 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17161a;
    --fg: #e9e6df;
    --muted: #99928a;
    --rule: #35302a;
    --accent: #e4a66e;
    --attention: #e0a864;
    --warning: #d97b6c;
    --code-bg: #232025;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); }
body {
  font-family: var(--serif);
  font-size: 17px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
.layout { display: grid; grid-template-columns: 260px 1fr; min-height: 100vh; max-width: 1200px; margin: 0 auto; }
nav.sidebar {
  border-right: 1px solid var(--rule);
  padding: 32px 20px 40px;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
  background: var(--bg);
}
main.content { padding: 40px 48px 80px; min-width: 0; }
header.masthead {
  border-bottom: 2px solid var(--fg);
  padding-bottom: 16px;
  margin-bottom: 32px;
}
header.masthead .kicker {
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 6px;
}
header.masthead h1 {
  font-family: var(--serif);
  font-weight: 700;
  font-size: 42px;
  line-height: 1.05;
  margin: 0 0 8px;
  letter-spacing: -0.01em;
}
header.masthead .dateline {
  font-family: var(--sans);
  font-size: 13px;
  color: var(--muted);
}

nav.sidebar .toc-title {
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 12px;
}
nav.sidebar ul.toc-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
nav.sidebar ul.toc-list li { margin: 0; }
nav.sidebar ul.toc-list a {
  display: block;
  padding: 7px 10px;
  margin: 1px 0;
  color: var(--fg);
  text-decoration: none;
  font-family: var(--sans);
  font-size: 14px;
  border-radius: 6px;
  border-left: 2px solid transparent;
  transition: background 120ms, border-color 120ms;
}
nav.sidebar ul.toc-list a:hover { background: var(--code-bg); }
nav.sidebar .toc-level-1 > a { font-weight: 700; font-size: 15px; }
nav.sidebar .toc-level-2 > a { padding-left: 18px; font-size: 13.5px; }
nav.sidebar .toc-callout-attention > a { border-left-color: var(--attention); color: var(--attention); }
nav.sidebar .toc-callout-warning > a { border-left-color: var(--warning); color: var(--warning); }
.toc-callout-icon { margin-right: 4px; }

main.content h1, main.content h2, main.content h3 { font-family: var(--serif); }
main.content h2 {
  font-size: 28px;
  line-height: 1.15;
  margin: 40px 0 12px;
  padding-top: 20px;
  border-top: 1px solid var(--rule);
  scroll-margin-top: 20px;
}
main.content h2:first-of-type { border-top: none; padding-top: 0; margin-top: 0; }
main.content h2[data-callout="attention"] { color: var(--attention); }
main.content h2[data-callout="warning"] { color: var(--warning); }
main.content h2 .h-icon { margin-right: 6px; font-size: 22px; }
main.content h3 { font-size: 20px; margin: 28px 0 10px; }
main.content p { margin: 0 0 14px; }
main.content ul, main.content ol { margin: 0 0 14px; padding-left: 24px; }
main.content li { margin: 4px 0; }
main.content li > p { margin-bottom: 4px; }
main.content strong { color: var(--accent); font-weight: 700; }
main.content em { font-style: italic; }
main.content blockquote {
  border-left: 3px solid var(--rule);
  padding: 6px 16px;
  margin: 16px 0;
  color: var(--muted);
  font-style: italic;
}
main.content code {
  font-family: var(--mono);
  font-size: 14px;
  background: var(--code-bg);
  padding: 2px 5px;
  border-radius: 4px;
}
main.content pre {
  background: var(--code-bg);
  padding: 14px 18px;
  border-radius: 8px;
  overflow-x: auto;
  font-size: 13.5px;
  line-height: 1.45;
  margin: 14px 0;
}
main.content pre code { background: transparent; padding: 0; }
main.content table {
  border-collapse: collapse;
  width: 100%;
  margin: 16px 0;
  font-family: var(--sans);
  font-size: 14px;
}
main.content th, main.content td {
  text-align: left;
  padding: 8px 12px;
  border-bottom: 1px solid var(--rule);
}
main.content th {
  background: var(--code-bg);
  font-weight: 700;
  font-size: 12px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
}
main.content a { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
main.content hr { border: none; border-top: 1px solid var(--rule); margin: 28px 0; }

footer.briefing-footer {
  margin-top: 60px;
  padding-top: 20px;
  border-top: 1px solid var(--rule);
  font-family: var(--sans);
  font-size: 12px;
  color: var(--muted);
}

button.nav-toggle {
  display: none;
  position: fixed;
  top: 12px;
  left: 12px;
  z-index: 20;
  background: var(--bg);
  border: 1px solid var(--rule);
  border-radius: 6px;
  padding: 8px 12px;
  font-family: var(--sans);
  font-size: 14px;
  cursor: pointer;
  color: var(--fg);
}

@media (max-width: 820px) {
  .layout { grid-template-columns: 1fr; }
  nav.sidebar {
    position: fixed;
    top: 0; left: 0;
    width: 280px;
    height: 100vh;
    z-index: 10;
    transform: translateX(-100%);
    transition: transform 200ms ease;
    box-shadow: 2px 0 12px rgba(0,0,0,0.15);
  }
  nav.sidebar.open { transform: translateX(0); }
  main.content { padding: 60px 20px 60px; }
  header.masthead h1 { font-size: 32px; }
  button.nav-toggle { display: block; }
}
"""

JS = """
(function () {
  var toggle = document.querySelector('button.nav-toggle');
  var sidebar = document.querySelector('nav.sidebar');
  if (toggle && sidebar) {
    toggle.addEventListener('click', function () {
      sidebar.classList.toggle('open');
    });
    sidebar.addEventListener('click', function (e) {
      if (e.target.tagName === 'A') {
        sidebar.classList.remove('open');
      }
    });
  }
})();
"""


def render_html(
    *,
    title: str,
    kicker: str,
    dateline: str,
    toc_html: str,
    body_html: str,
) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
{CSS}
</style>
</head>
<body>
<button class="nav-toggle" aria-label="Toggle navigation">☰ Sections</button>
<div class="layout">
  <nav class="sidebar" aria-label="Section navigation">
    <p class="toc-title">Sections</p>
    {toc_html}
  </nav>
  <main class="content" role="main">
    <header class="masthead">
      <div class="kicker">{html.escape(kicker)}</div>
      <h1>{html.escape(title)}</h1>
      <div class="dateline">{html.escape(dateline)}</div>
    </header>
    <div class="briefing-body">
      {body_html}
    </div>
    <footer class="briefing-footer">
      Generated by ${INSTANCE_NAME} for the installer. Source Markdown stays in ${CHASSIS_HOME}/briefings/ for grep + audit.
    </footer>
  </main>
</div>
<script>{JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Title / kicker inference
# ---------------------------------------------------------------------------

FIRST_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def infer_title(md_src: str, fallback: str) -> tuple[str, str]:
    m = FIRST_H1_RE.search(md_src)
    if m:
        h1 = m.group(1).strip()
        # Expect patterns like "Morning Briefing - 2026-04-19" or "Weekly Rollup - ..."
        parts = re.split(r"\s*[—\-]\s*", h1, maxsplit=1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return h1, fallback
    return fallback, ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Convert a briefing markdown file to a newspaper-style HTML file.")
    ap.add_argument("input", help="Path to the markdown file")
    ap.add_argument("--output", help="Output HTML path (defaults to same name, .html extension)")
    ap.add_argument("--title", help="Override the title (otherwise inferred from first H1)")
    ap.add_argument("--kicker", help="Override the kicker (small text above title)")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        print(f"ERROR: {src} not found", file=sys.stderr)
        return 1

    md_src = src.read_text(encoding="utf-8")

    inferred_title, inferred_date = infer_title(md_src, fallback=src.stem)
    title = args.title or inferred_title
    kicker = args.kicker or os.environ.get("INSTANCE_NAME", "Behalf.bot") + " Briefing"
    dateline = inferred_date or src.stem

    sections = extract_sections(md_src)
    toc_html = build_toc(sections)
    body_html = render_body(md_src)

    html_out = render_html(
        title=title,
        kicker=kicker,
        dateline=dateline,
        toc_html=toc_html,
        body_html=body_html,
    )

    out_path = Path(args.output) if args.output else src.with_suffix(".html")
    out_path.write_text(html_out, encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
