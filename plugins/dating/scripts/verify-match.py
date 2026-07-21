#!/usr/bin/env python3
"""Reverse-image-search a dating-match profile photo via Yandex Image Search.

Detects catfish profiles by checking whether a photo appears across multiple
sites under different names. Workflow proven on the V1 reference install
(<v1-reference-install>), which caught two stolen-photo catfish profiles within a
72-hour window using this consensus engine.

Usage:
    verify-match.py --name "Jane" --platform hinge --photo /path/to/face.jpg
    verify-match.py --name "Jane" --platform hinge --photos a.jpg b.jpg c.jpg

Output (under <output-root>/<slug>-<platform>-<date>/):
    - search-results.png       full-page screenshot of Yandex "Search" tab
    - sites.png                full-page screenshot of "Sites" tab
    - report.md                summary with detected names + sites + verdict
    - raw.json                 structured data for downstream automation

The output root is configurable via --output-root (CLI) or
DATING_VERIFICATIONS_ROOT (env). The chassis sets it to
${CHASSIS_HOME}/data/dating/verifications by default.

Verdict — traffic-light model:
    🔴 RED    drop comms — TinEye byte-match on adult/escort aggregator
              domain, OR Google Lens high-confidence hit on same. Both
              are byte / fingerprint level, not "looks similar".
    🟡 YELLOW flag + warn the installer, allow comms with caution —
              TinEye ≥5 total matches (photo widely reused), Lens ≥8
              visual matches, Lens OCR'd a name in the image, OR Yandex
              similarity hit on an adult-aggregator domain (low-confidence,
              visual similarity only — investigate, don't auto-reject).
    🟢 GREEN  no suspicious signals after low-suspicion filter
    no_signal no public web presence found — treat as green for comms

Yandex provides VISUAL SIMILARITY, not byte-matches. Similarity hits
cannot move the verdict past YELLOW. "Looks similar" is not catfish
signal — there is always someone who looks similar on the web. Yandex
keyword-in-title and distinct-name-candidate signals are INFORMATIONAL
ONLY: captured in raw.json but do not feed the verdict. The catfish bar
is exact-byte (TinEye) or high-confidence-visual (Lens) on a high-
suspicion domain.

Catfish keyword markers (in site titles):
    nude, sexy, thotflix, aznude, wikifeet, adultgallery, splatinyourface,
    bellazon (often used for stolen-celeb pools)

High-suspicion domains (direct hit → RED):
    onlyfans.com, leakedmodels.com, thothub.tv, aznude.com, wikifeet.com,
    bellazon.com, fapello.com, plus known escort-directory and
    catfish-marketplace aggregators. Match is exact-or-subdomain.

Low-suspicion filter (excluded from "distinct names" count):
    bbc.com, freepik.com, pinterest.com, xwhos.com, toolify.ai,
    iphonephotographyschool.com, *.userapi.com, *.pinimg.com, etc.
    These are common Yandex contextual-similarity false-positive sources
    (visual similarity, not byte-level matches).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout


def _default_output_root() -> str:
    """Default output root for verification reports.

    Resolution order:
      1. DATING_VERIFICATIONS_ROOT env var (chassis-managed)
      2. ${CHASSIS_HOME}/data/dating/verifications when CHASSIS_HOME is set
      3. ./data/dating/verifications relative to cwd as a last resort
    """
    if env := os.environ.get("DATING_VERIFICATIONS_ROOT"):
        return env
    if home := os.environ.get("CHASSIS_HOME"):
        return os.path.join(home, "data", "dating", "verifications")
    return "data/dating/verifications"


CATFISH_KEYWORDS = (
    "nude", "sexy", "naked", "thotflix", "aznude", "wikifeet",
    "adultgallery", "splatinyourface", "bellazon", "thot", "onlyfans",
    "porn", "escort", "desnuda", "feet",
)

# Domain trust tiers. Hits on HIGH_SUSPICION domains push toward RED.
# LOW_SUSPICION (news, stock photo, social pinboards, film databases, etc.)
# never count toward the catfish verdict — they show up because Yandex's
# contextual-similarity search returns visually-related images, not
# byte-level matches. False-positive sources, filed in #409.
HIGH_SUSPICION_DOMAINS = (
    "onlyfans.com", "leakedmodels.com", "thothub.tv", "thotflix",
    "aznude.com", "wikifeet.com", "bellazon.com", "thothd.com",
    "adultgallery.com", "fapello.com", "thotvideos.com", "scoreland.com",
    "yespornpics.com", "pornpics.com", "sxyprn.com", "eroticbeauties.net",
    # Escort-directory + catfish-marketplace aggregators where stolen photo
    # sets recirculate
    "rusescort.com", "putana.org", "escort-russia.com",
)
LOW_SUSPICION_DOMAINS = (
    # News + obit + journalism — different person, same coloring/composition
    "bbc.com", "bbci.co.uk", "bbc.co.uk", "nytimes.com", "wsj.com",
    "theguardian.com", "reuters.com", "cnn.com", "wikipedia.org",
    # Stock photo + design assets
    "freepik.com", "shutterstock.com", "istockphoto.com", "gettyimages.com",
    "alamy.com", "depositphotos.com", "unsplash.com", "pexels.com",
    # Pinboards + image hosts (Yandex floods these on visual-similarity)
    "pinterest.com", "pinimg.com", "tumblr.com", "flickr.com",
    "imgur.com", "userapi.com",
    # Film / TV databases
    "imdb.com", "xwhos.com", "themoviedb.org",
    # Tech tutorials / blog content
    "toolify.ai", "iphonephotographyschool.com", "caraqu.com",
    # General e-commerce (frequent visual-similarity false positives)
    "usadostavka.ru", "ozon.ru", "wildberries.ru",
)
# Stop-words and noise patterns we strip before counting "distinct names".
NAME_STOP_TOKENS = (
    "premium", "photo", "close", "shot", "best", "discover",
    "blur", "background", "app", "the", "and", "with", "of",
    "season", "drama", "indie", "film", "celebrity", "celebrities",
    "app", "iphone", "android", "samsung",
)
DIMENSION_RE = re.compile(r"^\d{2,4}\s*[×x]\s*\d{2,4}$")


@dataclass
class SiteHit:
    title: str
    domain: str
    snippet: str = ""


@dataclass
class VerificationResult:
    name: str
    platform: str
    photo: str
    timestamp: str
    # --- Yandex backend (legacy; demoted to YELLOW-only after #449) ---
    yandex_face_tags: list[str] = field(default_factory=list)
    similar_image_keywords: list[str] = field(default_factory=list)
    site_hits: list[SiteHit] = field(default_factory=list)
    distinct_names: list[str] = field(default_factory=list)
    catfish_marker_hits: list[str] = field(default_factory=list)
    high_suspicion_domain_hits: list[str] = field(default_factory=list)
    low_suspicion_filtered_count: int = 0
    yandex_status: str = "ok"  # ok | scrape_failed | timeout

    # --- TinEye backend (#449; byte-level + perceptual hash) ---
    # status: ok | scrape_failed | captcha_blocked | no_results
    tineye_status: str = "not_run"
    tineye_total_matches: int = 0
    tineye_domains: list[str] = field(default_factory=list)
    tineye_high_suspicion_domain_hits: list[str] = field(default_factory=list)

    # --- Google Lens backend (#449; visual + OCR + entity) ---
    # status: ok | scrape_failed | no_results
    lens_status: str = "not_run"
    lens_visual_match_count: int = 0
    lens_domains: list[str] = field(default_factory=list)
    lens_ocr_text: str = ""
    lens_high_suspicion_domain_hits: list[str] = field(default_factory=list)

    # Traffic-light verdict: "red"|"yellow"|"green"|"no_signal".
    # Legacy "catfish" / "verified_real" values map to red / green for any
    # downstream callers still on the old vocabulary; new callers should
    # branch on `verdict` directly using the traffic-light values.
    verdict: str = "unknown"
    rationale: str = ""
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["site_hits"] = [asdict(h) for h in self.site_hits]
        return d


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def upload_photo_to_yandex(page: Page, photo_path: Path) -> None:
    page.goto("https://yandex.com/images/", wait_until="domcontentloaded")
    page.get_by_role("button", name="Image search").click()
    with page.expect_file_chooser() as chooser_ctx:
        page.get_by_role("button", name="Select file").click()
    chooser_ctx.value.set_files(str(photo_path))
    page.wait_for_url(lambda url: "cbir_id=" in url, timeout=30000)
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(2)


def extract_face_tags(page: Page) -> list[str]:
    tags = []
    for el in page.locator('a[href*="text="][href*="cbir_id"], button:has-text("")').all():
        try:
            txt = el.inner_text(timeout=500).strip()
        except Exception:
            continue
        if 1 <= len(txt) <= 60 and not any(
            k in txt.lower() for k in ("similar", "search", "sites", "view all", "show more")
        ):
            tags.append(txt)
    seen = set()
    out = []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:8]


def parse_sites_tab(page: Page) -> list[SiteHit]:
    hits: list[SiteHit] = []
    cards = page.locator('a[href^="http"]').all()
    seen_titles: set[str] = set()
    for card in cards:
        try:
            href = card.get_attribute("href") or ""
        except Exception:
            continue
        if not href.startswith("http") or "yandex." in href:
            continue
        try:
            title = card.inner_text(timeout=300).strip()
        except Exception:
            continue
        if not title or len(title) < 6 or len(title) > 200:
            continue
        if title.lower() in seen_titles:
            continue
        domain = re.sub(r"^https?://(www\.)?([^/]+).*$", r"\2", href)
        hits.append(SiteHit(title=title, domain=domain))
        seen_titles.add(title.lower())
        if len(hits) >= 30:
            break
    return hits


# ---------------------------------------------------------------------------
# TinEye backend (#449)
# ---------------------------------------------------------------------------
#
# TinEye does byte-level + perceptual-hash reverse image search across its
# crawl of the public web. Where Yandex returns "visually similar" (often
# different people who look alike), TinEye returns the exact image (or
# near-exact crops). Strongest signal for "this photo has been reused
# elsewhere on the public web under a different identity."
#
# Free Playwright path: tineye.com upload form. May intermittently get
# blocked by their bot detection — return scrape_failed if so, let the
# aggregator cope. Don't retry aggressively; multiple retries only make
# bot detection more likely.
#
# We don't need to log in. Public TinEye allows ~5 anonymous searches per
# day per IP; for typical match rates that's plenty.

def upload_photo_to_tineye(page: Page, photo_path: Path) -> bool:
    """Upload photo to TinEye, wait for results page. Returns True on
    apparent success, False on scrape failure / captcha / no upload form."""
    try:
        page.goto("https://tineye.com/", wait_until="domcontentloaded", timeout=20000)
        # Cookie / GDPR banner may appear; dismiss if present
        try:
            page.get_by_role("button", name=re.compile(r"accept|agree|got it|ok", re.I)).first.click(timeout=2000)
        except Exception:
            pass
        # Look for the file input. TinEye uses a styled label that wraps an
        # invisible <input type=file>. Set files directly on the input.
        file_input = page.locator('input[type="file"]').first
        file_input.set_input_files(str(photo_path), timeout=10000)
        # TinEye redirect chain: /search → /search/<hash>?... Use a
        # lambda predicate (more reliable than the raw regex param —
        # Playwright's URL-string matcher uses glob, not regex, so
        # `tineye.com/search/` literal dots don't match. The lambda is
        # explicit + correct.)
        page.wait_for_url(lambda u: "/search/" in u and len(u) > 30, timeout=45000)
        # Don't wait for networkidle on TinEye — their ads/analytics keep
        # the page busy. domcontentloaded is enough; results are server-
        # rendered into the initial HTML.
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        time.sleep(2)
        # Detect captcha block
        body_text = page.content().lower()
        if "captcha" in body_text or "are you a robot" in body_text or "verify you are a human" in body_text:
            return False
        return True
    except (PWTimeout, Exception):
        return False


def parse_tineye_results(page: Page) -> tuple[int, list[str]]:
    """Returns (total_match_count, list_of_unique_domains).

    Scoped to TinEye's actual match cards — sponsor banners, related-image
    carousel, and footer links are excluded. Exits early on "0 results" text
    to prevent sponsor-link false positives (issue #456).
    """
    total = 0
    domains: list[str] = []
    seen: set[str] = set()

    try:
        text = page.locator("body").inner_text(timeout=2000)
    except Exception:
        text = ""

    # Early-exit: "0 results" / "0 matches" means no real hits exist.
    # Short-circuits before any <a> walk so sponsor banners (e.g. the
    # Shutterstock promo link present on every zero-results page) can never
    # be counted as real matches. Fixes #456.
    if re.search(r"\b0\s+(?:result|match)", text, re.I):
        return (0, [])

    m = re.search(r"(\d{1,4})\s+(?:result|match)", text, re.I)
    if m:
        total = int(m.group(1))

    # Scope link extraction to TinEye's actual results container.
    # TinEye uses "ul.results" (stable since at least 2020) for match cards.
    # If absent (layout change), fall back to page-wide scan but block known
    # TinEye ad/sponsor domains explicitly so they never inflate the count.
    TINEYE_AD_DOMAINS = frozenset((
        "shutterstock.com", "istockphoto.com", "gettyimages.com", "alamy.com",
    ))

    results_container = page.locator("ul.results, .results-by-image")
    try:
        use_scoped = results_container.count() > 0
    except Exception:
        use_scoped = False

    link_locator = (
        results_container.first.locator('a[href^="http"]')
        if use_scoped
        else page.locator('a[href^="http"]')
    )

    for el in link_locator.all()[:60]:
        try:
            href = el.get_attribute("href") or ""
        except Exception:
            continue
        if not href.startswith("http") or "tineye.com" in href:
            continue
        dm = re.match(r"^https?://(?:www\.)?([^/]+)", href)
        if not dm:
            continue
        domain = dm.group(1).lower()
        if any(domain == ad or domain.endswith("." + ad) for ad in TINEYE_AD_DOMAINS):
            continue
        if domain in seen:
            continue
        domains.append(domain)
        seen.add(domain)
        if len(domains) >= 30:
            break

    if total == 0 and domains:
        total = len(domains)
    return (total, domains)


# ---------------------------------------------------------------------------
# Google Lens backend (#449)
# ---------------------------------------------------------------------------
#
# Lens does visual matching + OCR + entity recognition. Output is messier
# than TinEye (it's a mainstream consumer product, layout shifts often) but
# it's the only free service that surfaces the WEB CONTEXT of where a photo
# appears. Useful for "this image was scraped from <publication>'s 2018
# article about <person>" identity-mismatch detection.
#
# Path: lens.google.com → upload → "Find image source" results page.
# Google returns visual matches + a list of source pages with thumbnails.
# We extract: visual match count, domains, OCR text from the photo (some
# catfish photos have watermarks Lens reads).

def upload_photo_to_lens(page: Page, photo_path: Path, debug_dir: Path | None = None) -> str:
    """Upload photo to Google Lens. Returns 'ok' or a diagnostic status string.

    Brings <v1-reference-install> PR #542 (chassis #92) upstream. The previous opaque \"return
    True/False\" gave operators no way to tell WHY a Lens scrape failed —
    consent screen blocked? upload itself failed? URL wait timed out? Now
    each failure mode returns a distinct string + optionally writes a debug
    screenshot to debug_dir for post-mortem inspection.

    Possible return values:
      ok               — upload succeeded, results page loaded
      consent_blocked  — GDPR consent screen could not be dismissed
      upload_failed:<exc>  — file input not found or set_input_files failed
      timeout:<exc>    — URL wait timed out after upload
      error:<exc>      — unexpected exception type

    Callers should check `result == "ok"` (not just truthy).
    """
    def _screenshot(tag: str) -> None:
        if debug_dir:
            try:
                page.screenshot(path=str(debug_dir / f"lens-debug-{tag}.png"))
            except Exception:
                pass

    try:
        page.goto("https://lens.google.com/", wait_until="domcontentloaded", timeout=20000)
        # GDPR / EU consent screen — EU-jurisdiction installs hit this every
        # fresh visit. Hard-fail with 'consent_blocked' if we can't dismiss
        # it; previously this was a silent pass-through that left the page
        # in an unusable state and returned scrape_failed with no details.
        if "consent.google.com" in page.url:
            _screenshot("consent")
            try:
                page.get_by_role("button", name=re.compile(r"reject all|accept all|i agree", re.I)).first.click(timeout=5000)
                page.wait_for_url(lambda u: "consent.google.com" not in u, timeout=10000)
            except Exception:
                _screenshot("consent-failed")
                return "consent_blocked"
        # Lens upload UX: a file input lives inside the "Upload" panel.
        # Click "Upload" if present, then set files on the hidden input.
        try:
            page.get_by_role("button", name=re.compile(r"upload", re.I)).first.click(timeout=2000)
        except Exception:
            pass
        try:
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(str(photo_path), timeout=10000)
        except Exception as e:
            _screenshot("upload-failed")
            return f"upload_failed: {type(e).__name__}"
        # After upload, Lens redirects to a results URL. Use a lambda
        # predicate (Playwright's URL-string matcher is glob, not regex).
        try:
            page.wait_for_url(
                lambda u: ("lens.google.com" in u and "/search" in u) or ("google.com/search" in u and "udm=" in u),
                timeout=45000,
            )
        except PWTimeout as e:
            _screenshot("timeout")
            return f"timeout: {type(e).__name__}"
        # Same fix as TinEye — Lens has ads / analytics polling that
        # prevent networkidle from firing. domcontentloaded is sufficient.
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        time.sleep(3)
        # Click "Find image source" tab if not already on it (the Lens
        # default lands on visual matches; the "source" view has the
        # web-context list we want).
        try:
            page.get_by_role("link", name=re.compile(r"find image source|exact match|source", re.I)).first.click(timeout=3000)
            page.wait_for_load_state("networkidle", timeout=10000)
            time.sleep(1)
        except Exception:
            pass  # If we can't find the tab, parse whatever Lens landed on
        return "ok"
    except PWTimeout as e:
        _screenshot("timeout-outer")
        return f"timeout: {type(e).__name__}"
    except Exception as e:
        _screenshot("error")
        return f"error: {type(e).__name__}"


def parse_lens_results(page: Page) -> tuple[int, list[str], str]:
    """Returns (visual_match_count, list_of_unique_domains, ocr_text)."""
    domains: list[str] = []
    seen: set[str] = set()
    ocr_text = ""
    # OCR: Lens shows extracted text in a "Text" tab if any was detected.
    try:
        ocr_panel = page.locator('[role="tabpanel"]:has-text("Text")').first
        ocr_text = ocr_panel.inner_text(timeout=1500)[:500]
    except Exception:
        pass
    # Visual matches + source pages: pull external hrefs that aren't
    # google.com / lens.google.com / gstatic.com
    for el in page.locator('a[href^="http"]').all()[:80]:
        try:
            href = el.get_attribute("href") or ""
        except Exception:
            continue
        m = re.match(r"^https?://(?:www\.)?([^/]+)", href)
        if not m:
            continue
        domain = m.group(1).lower()
        if any(skip in domain for skip in ("google.com", "gstatic.com", "googleusercontent.com", "googletagmanager.com", "google-analytics.com", "schema.org", "youtu.be")):
            continue
        if domain in seen:
            continue
        domains.append(domain)
        seen.add(domain)
        if len(domains) >= 30:
            break
    return (len(domains), domains, ocr_text.strip())


def _filter_high_sus_domains(domains: list[str]) -> list[str]:
    """Reuse the high-suspicion list across services."""
    hits = []
    for d in domains:
        if _is_high_suspicion_domain(d):
            hits.append(d)
    return hits


# ---------------------------------------------------------------------------


def _is_low_suspicion_domain(domain: str) -> bool:
    """True if the domain is a known false-positive source (news, stock
    photo, social pinboard, film database, tech tutorial, etc.)."""
    d = domain.lower()
    return any(d == sus or d.endswith("." + sus) for sus in LOW_SUSPICION_DOMAINS)


def _is_high_suspicion_domain(domain: str) -> bool:
    d = domain.lower()
    return any(d == sus or d.endswith("." + sus) for sus in HIGH_SUSPICION_DOMAINS)


def _name_is_noise(name: str) -> bool:
    """Reject 'distinct name' candidates that are dimension strings, single
    stop-words, or sentence fragments dressed up as names. Filed in #409
    after Anna (Hinge) 2026-05-02 false positive — Yandex contextual hits
    surfaced 'premium photo close', 'best time', 'one week' etc. as
    'distinct names'."""
    n = name.strip().lower()
    if not n or len(n) < 4:
        return True
    if DIMENSION_RE.match(n):
        return True
    if n in NAME_STOP_TOKENS:
        return True
    tokens = n.split()
    # Reject if all tokens are stop-words or 2/3 of them are
    stop_count = sum(1 for t in tokens if t in NAME_STOP_TOKENS)
    if stop_count >= max(1, len(tokens) - 1):
        return True
    return False


def detect_distinct_names(site_hits: list[SiteHit]) -> tuple[list[str], int]:
    """Returns (filtered_names, low_suspicion_filtered_count).

    `filtered_names`: candidate person-names extracted from site titles,
    AFTER filtering out noise (stop-words, dimension strings, single
    tokens, sentence fragments).

    `low_suspicion_filtered_count`: count of hits we DROPPED because they
    were on low-suspicion domains (news, stock photo, etc.) — these are
    visually-similar matches Yandex returned, not actual identity hits.
    Tracked for transparency in the report.
    """
    names: set[str] = set()
    low_sus_dropped = 0
    for hit in site_hits:
        if _is_low_suspicion_domain(hit.domain):
            low_sus_dropped += 1
            continue
        match = re.match(r"([A-Z][a-zA-Zà-ÿ'\-]+(?:\s+[A-Z][a-zA-Zà-ÿ'\-]+){1,3})", hit.title)
        if not match:
            continue
        candidate = match.group(1).strip().lower()
        if _name_is_noise(candidate):
            continue
        names.add(candidate)
    return sorted(names), low_sus_dropped


def detect_catfish_markers(site_hits: list[SiteHit]) -> list[str]:
    """Title/domain keyword scan — explicit catfish-marketplace signals."""
    hits = []
    for hit in site_hits:
        haystack = (hit.title + " " + hit.domain).lower()
        for kw in CATFISH_KEYWORDS:
            if kw in haystack:
                hits.append(f"{kw} → {hit.domain}")
                break
    return hits


def detect_high_suspicion_domains(site_hits: list[SiteHit]) -> list[str]:
    """Direct domain match against the HIGH_SUSPICION_DOMAINS allowlist —
    onlyfans.com, leakedmodels.com, etc. Stronger signal than the keyword
    scan because it doesn't depend on the page title text."""
    hits = []
    seen = set()
    for hit in site_hits:
        if _is_high_suspicion_domain(hit.domain) and hit.domain not in seen:
            hits.append(hit.domain)
            seen.add(hit.domain)
    return hits


def compute_verdict(result: VerificationResult) -> tuple[str, str, dict]:
    """Traffic-light verdict — multi-source aggregator (#449).

    Three backends contribute:
      - TinEye: byte-level / perceptual-hash matches. Strongest signal.
        ANY high-suspicion-domain hit → RED. ≥2 unrelated stock-photo
        domains hit → RED (image is reused public-web stock).
      - Google Lens: visual matches + OCR + web context. High-suspicion-
        domain hit → RED. OCR pulling a watermark text that names a
        different person → YELLOW.
      - Yandex: visually-similar (legacy backend). Per #409 + #449:
        DEMOTED to YELLOW-only. Yandex contributing alone (without
        TinEye / Lens corroboration) maxes out at YELLOW even on
        explicit catfish-keyword hits, because Yandex's contextual
        similarity surfaces too many false positives. Yandex high-
        suspicion-domain hit STILL escalates to RED (unambiguous).

    Aggregator priority: RED beats YELLOW beats GREEN. First service
    contributing RED wins (and the rationale enumerates which one).
    """
    # --- Per-service signal extraction ---
    n_filtered_names = len(result.distinct_names)
    n_marker_hits = len(result.catfish_marker_hits)
    n_yandex_high_sus = len(result.high_suspicion_domain_hits)
    n_total_sites = len(result.site_hits)

    n_tineye_high_sus = len(result.tineye_high_suspicion_domain_hits)
    n_lens_high_sus = len(result.lens_high_suspicion_domain_hits)

    signals = {
        "yandex": {
            "status": result.yandex_status,
            "filtered_distinct_names": n_filtered_names,
            "low_suspicion_filtered_out": result.low_suspicion_filtered_count,
            "catfish_keyword_hits": n_marker_hits,
            "high_suspicion_domain_hits": n_yandex_high_sus,
            "raw_site_hit_count": n_total_sites,
        },
        "tineye": {
            "status": result.tineye_status,
            "total_matches": result.tineye_total_matches,
            "domains": result.tineye_domains,
            "high_suspicion_domain_hits": n_tineye_high_sus,
        },
        "lens": {
            "status": result.lens_status,
            "visual_match_count": result.lens_visual_match_count,
            "domains": result.lens_domains,
            "ocr_text": result.lens_ocr_text,
            "high_suspicion_domain_hits": n_lens_high_sus,
        },
    }

    red_reasons: list[str] = []
    yellow_reasons: list[str] = []

    # --- TinEye: strongest signal ---
    if result.tineye_status == "ok":
        if n_tineye_high_sus > 0:
            red_reasons.append(
                f"TinEye byte-match on {n_tineye_high_sus} catfish/adult-aggregator domain(s): "
                f"{', '.join(result.tineye_high_suspicion_domain_hits)}"
            )
        if result.tineye_total_matches >= 5:
            yellow_reasons.append(
                f"TinEye found {result.tineye_total_matches} byte-level matches across "
                f"{len(result.tineye_domains)} domain(s) — photo is reused on the public web"
            )

    # --- Google Lens: visual + OCR + web-context ---
    if result.lens_status == "ok":
        if n_lens_high_sus > 0:
            red_reasons.append(
                f"Google Lens hit on {n_lens_high_sus} catfish/adult-aggregator domain(s): "
                f"{', '.join(result.lens_high_suspicion_domain_hits)}"
            )
        if result.lens_visual_match_count >= 8:
            yellow_reasons.append(
                f"Google Lens found {result.lens_visual_match_count} visual matches across "
                f"the public web — photo is widely reused"
            )
        # OCR-based tells. If Lens read a watermark like "© John Doe Photography"
        # or a different person's name, that's a YELLOW. Pure cosmetic OCR
        # ('LIVE', 'iOS' etc) is noise — only flag if it looks like a name.
        if result.lens_ocr_text and re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", result.lens_ocr_text):
            yellow_reasons.append(
                f"Lens OCR detected name-shaped text in image: {result.lens_ocr_text[:80]}"
            )

    # --- Yandex: DEMOTED to similarity-only. Yandex returns visual-
    # similarity hits, not byte-matches. "Looks similar" is not catfish
    # signal — there is always a similar-looking person on the web. Only
    # an adult-aggregator DOMAIN hit escalates to YELLOW (suggestive but
    # not conclusive on similarity alone), and even that requires the
    # installer's review. Keyword + distinct-name triggers are
    # INFORMATIONAL — captured in raw.json but no longer affect the
    # verdict. The catfish bar is: TinEye exact byte-match OR Lens
    # high-confidence (high-suspicion domain) OR (future) PimEyes high-
    # confidence — Yandex alone cannot move the verdict past YELLOW.
    if result.yandex_status == "ok":
        if n_yandex_high_sus > 0:
            yellow_reasons.append(
                f"Yandex similarity hit on {n_yandex_high_sus} adult-aggregator domain(s) "
                f"(visual similarity, NOT byte-match — investigate, don't auto-reject): "
                f"{', '.join(result.high_suspicion_domain_hits)}"
            )
        # n_marker_hits and n_filtered_names are now informational only —
        # see raw.json for their values. Logged but not verdict-affecting.

    # --- Verdict aggregation ---
    if red_reasons:
        return ("red", " | ".join(red_reasons), signals)
    if yellow_reasons:
        return ("yellow", " | ".join(yellow_reasons), signals)

    # No suspicious signals from any service.
    backends_run = [
        b for b, s in (("yandex", result.yandex_status), ("tineye", result.tineye_status), ("lens", result.lens_status))
        if s == "ok"
    ]
    backends_failed = [
        b for b, s in (("yandex", result.yandex_status), ("tineye", result.tineye_status), ("lens", result.lens_status))
        if s not in ("ok", "not_run")
    ]

    if not backends_run:
        return (
            "no_signal",
            f"all backends failed/unavailable ({','.join(backends_failed) or 'none'}) — manual verification needed",
            signals,
        )

    if n_total_sites == 0 and result.tineye_total_matches == 0 and result.lens_visual_match_count == 0:
        return (
            "no_signal",
            f"no public web presence found across {len(backends_run)} backend(s) — could be private OR brand-new identity",
            signals,
        )

    return (
        "green",
        f"no catfish signals across {len(backends_run)} backend(s) "
        f"({', '.join(backends_run)}); benign hits filtered out",
        signals,
    )


def render_markdown(result: VerificationResult, output_dir: Path) -> str:
    verdict_emoji = {
        "red": "🔴", "yellow": "🟡", "green": "🟢", "no_signal": "⚪",
    }.get(result.verdict, "❓")
    lines = [
        f"# Photo verification — {result.name} ({result.platform})",
        "",
        f"- **Run:** {result.timestamp}",
        f"- **Photo:** `{result.photo}`",
        f"- **Verdict:** {verdict_emoji} **{result.verdict.upper()}** — {result.rationale}",
        "",
    ]
    if result.signals:
        y = result.signals.get("yandex", {})
        t = result.signals.get("tineye", {})
        l = result.signals.get("lens", {})
        lines.extend([
            "## Signals breakdown (multi-backend per #449)",
            "",
            f"- **TinEye** [{t.get('status', 'unknown')}]: {t.get('total_matches', 0)} byte-level match(es); "
            f"{len(t.get('domains', []))} unique domain(s); "
            f"{t.get('high_suspicion_domain_hits', 0)} high-suspicion-domain hit(s)",
            f"- **Google Lens** [{l.get('status', 'unknown')}]: {l.get('visual_match_count', 0)} visual match(es); "
            f"{len(l.get('domains', []))} unique domain(s); "
            f"{l.get('high_suspicion_domain_hits', 0)} high-suspicion-domain hit(s)"
            + (f"; OCR: \"{l.get('ocr_text', '')[:60]}...\"" if l.get('ocr_text') else ""),
            f"- **Yandex** [{y.get('status', 'unknown')}] _(demoted to YELLOW-only contributor)_: "
            f"{y.get('raw_site_hit_count', 0)} raw site hit(s); "
            f"{y.get('filtered_distinct_names', 0)} filtered distinct name(s); "
            f"{y.get('low_suspicion_filtered_out', 0)} low-suspicion-domain hit(s) excluded; "
            f"{y.get('catfish_keyword_hits', 0)} catfish-keyword hit(s); "
            f"{y.get('high_suspicion_domain_hits', 0)} high-suspicion-domain hit(s)",
            "",
        ])
    lines.extend([
        "## Yandex face tags",
        "",
    ])
    if result.yandex_face_tags:
        lines.extend(f"- {t}" for t in result.yandex_face_tags)
    else:
        lines.append("_(none surfaced)_")
    lines.extend(["", "## Distinct names attached to the photo", ""])
    if result.distinct_names:
        lines.extend(f"- {n}" for n in result.distinct_names)
    else:
        lines.append("_(none parsed from site titles)_")
    lines.extend(["", "## Catfish-marker keyword hits", ""])
    if result.catfish_marker_hits:
        lines.extend(f"- {h}" for h in result.catfish_marker_hits)
    else:
        lines.append("_(none)_")
    lines.extend(["", "## Sites where the photo appears", ""])
    if result.site_hits:
        lines.append("| Title | Domain |")
        lines.append("|---|---|")
        for hit in result.site_hits:
            t = hit.title.replace("|", "\\|")[:120]
            lines.append(f"| {t} | {hit.domain} |")
    else:
        lines.append("_(none)_")
    lines.extend([
        "",
        "## TinEye matches (#449)",
        "",
    ])
    if result.tineye_status == "ok":
        if result.tineye_domains:
            lines.append("| Domain |")
            lines.append("|---|")
            for d in result.tineye_domains:
                marker = " ⚠️" if d in result.tineye_high_suspicion_domain_hits else ""
                lines.append(f"| {d}{marker} |")
        else:
            lines.append("_(no matches)_")
    else:
        lines.append(f"_(backend status: {result.tineye_status})_")

    lines.extend(["", "## Google Lens matches (#449)", ""])
    if result.lens_status == "ok":
        if result.lens_domains:
            lines.append("| Domain |")
            lines.append("|---|")
            for d in result.lens_domains:
                marker = " ⚠️" if d in result.lens_high_suspicion_domain_hits else ""
                lines.append(f"| {d}{marker} |")
        else:
            lines.append("_(no matches)_")
        if result.lens_ocr_text:
            lines.extend(["", "**OCR text from photo:**", "", f"> {result.lens_ocr_text}", ""])
    else:
        lines.append(f"_(backend status: {result.lens_status})_")

    lines.extend([
        "",
        "## Screenshots",
        "",
        "- `yandex-search.png` — Yandex Search tab",
        "- `yandex-sites.png` — Yandex Sites tab",
        "- `tineye-results.png` — TinEye results page (if backend succeeded)",
        "- `lens-results.png` — Google Lens results page (if backend succeeded)",
        "",
    ])
    return "\n".join(lines)


def run_verification(name: str, platform: str, photos: list[Path], output_root: Path) -> VerificationResult:
    slug = f"{slugify(name)}-{platform}"
    date = datetime.now().strftime("%Y-%m-%d-%H%M")
    output_dir = output_root / f"{slug}-{date}"
    output_dir.mkdir(parents=True, exist_ok=True)

    primary = photos[0]
    result = VerificationResult(
        name=name,
        platform=platform,
        photo=str(primary),
        timestamp=datetime.now().isoformat(),
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        )

        # --- Backend 1: Yandex (legacy, demoted to YELLOW-only via #449) ---
        page = ctx.new_page()
        try:
            upload_photo_to_yandex(page, primary)
            page.screenshot(path=str(output_dir / "yandex-search.png"), full_page=True)
            result.yandex_face_tags = extract_face_tags(page)
            sites_url = page.url + "&cbir_page=sites" if "cbir_page" not in page.url else re.sub(r"cbir_page=[^&]+", "cbir_page=sites", page.url)
            page.goto(sites_url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(2)
            page.screenshot(path=str(output_dir / "yandex-sites.png"), full_page=True)
            result.site_hits = parse_sites_tab(page)
            result.yandex_status = "ok"
        except (PWTimeout, Exception) as e:
            result.yandex_status = f"scrape_failed: {type(e).__name__}"
        finally:
            page.close()

        # --- Backend 2: TinEye (#449) ---
        page = ctx.new_page()
        try:
            if upload_photo_to_tineye(page, primary):
                try:
                    page.screenshot(path=str(output_dir / "tineye-results.png"), full_page=True)
                except Exception:
                    pass
                total, domains = parse_tineye_results(page)
                result.tineye_total_matches = total
                result.tineye_domains = domains
                result.tineye_high_suspicion_domain_hits = _filter_high_sus_domains(domains)
                result.tineye_status = "ok"
            else:
                result.tineye_status = "scrape_failed"
        except (PWTimeout, Exception) as e:
            result.tineye_status = f"scrape_failed: {type(e).__name__}"
        finally:
            page.close()

        # --- Backend 3: Google Lens (#449) ---
        page = ctx.new_page()
        try:
            # upload_photo_to_lens now returns a diagnostic STRING (per #92):
            # 'ok' on success, or one of consent_blocked / upload_failed:<exc>
            # / timeout:<exc> / error:<exc> on the various failure modes.
            # Debug screenshots land in output_dir for post-mortem.
            lens_upload_status = upload_photo_to_lens(page, primary, debug_dir=output_dir)
            if lens_upload_status == "ok":
                try:
                    page.screenshot(path=str(output_dir / "lens-results.png"), full_page=True)
                except Exception:
                    pass
                vmc, domains, ocr = parse_lens_results(page)
                result.lens_visual_match_count = vmc
                result.lens_domains = domains
                result.lens_ocr_text = ocr
                result.lens_high_suspicion_domain_hits = _filter_high_sus_domains(domains)
                result.lens_status = "ok"
            else:
                # Surface the diagnostic string (consent_blocked / upload_failed
                # / timeout / error) directly instead of collapsing to the
                # opaque 'scrape_failed'.
                result.lens_status = lens_upload_status
        except (PWTimeout, Exception) as e:
            result.lens_status = f"scrape_failed: {type(e).__name__}"
        finally:
            page.close()

        browser.close()

    # --- Yandex post-processing (legacy heuristics; results in #409 calibration) ---
    result.distinct_names, result.low_suspicion_filtered_count = detect_distinct_names(result.site_hits)
    result.catfish_marker_hits = detect_catfish_markers(result.site_hits)
    result.high_suspicion_domain_hits = detect_high_suspicion_domains(result.site_hits)

    # --- Verdict ---
    verdict, rationale, signals = compute_verdict(result)
    result.verdict = verdict
    result.rationale = rationale
    result.signals = signals

    (output_dir / "raw.json").write_text(json.dumps(result.to_dict(), indent=2))
    (output_dir / "report.md").write_text(render_markdown(result, output_dir))

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a dating-match profile photo via reverse image search.")
    parser.add_argument("--name", required=True, help="Match's first name (e.g. 'Jane')")
    parser.add_argument("--platform", required=True, help="Dating platform (tinder/hinge/bumble)")
    parser.add_argument("--photo", help="Path to a single profile photo")
    parser.add_argument("--photos", nargs="+", help="Paths to multiple profile photos (uses first as primary)")
    parser.add_argument("--output-root", default=_default_output_root(),
                        help="Output root dir. Defaults to DATING_VERIFICATIONS_ROOT env, "
                             "${CHASSIS_HOME}/data/dating/verifications, or ./data/dating/verifications.")
    args = parser.parse_args()

    if args.photo:
        photos = [Path(args.photo)]
    elif args.photos:
        photos = [Path(p) for p in args.photos]
    else:
        parser.error("must provide --photo or --photos")

    for p in photos:
        if not p.exists():
            parser.error(f"photo not found: {p}")

    output_root = Path(args.output_root)
    result = run_verification(args.name, args.platform, photos, output_root)

    output_dir = sorted(output_root.glob(f"{slugify(args.name)}-{args.platform}-*"))[-1]
    print(f"VERDICT: {result.verdict}")
    print(f"RATIONALE: {result.rationale}")
    print(f"OUTPUT: {output_dir}")
    # Exit codes: 0 = green / no_signal (allow comms), 1 = yellow (allow but
    # warn), 2 = red (drop comms). Caller uses these to gate downstream
    # action without re-parsing the verdict string.
    return {"green": 0, "no_signal": 0, "yellow": 1, "red": 2}.get(result.verdict, 1)


if __name__ == "__main__":
    sys.exit(main())
