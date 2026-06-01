#!/usr/bin/env bash
# <assistant>-collab.sh — CLI wrapper for OTA read/write against the
# "Sean ${ASSISTANT_NAME} collab" reMarkable notebook (or any other notebook by name).
#
# Use cases:
#   - Sean wants ${ASSISTANT_NAME} to read what's on a specific page of the collab notebook
#     before responding (e.g. annotations he made on a research brief)
#   - Sean wants ${ASSISTANT_NAME} to write a one-line response into the notebook so the
#     thread of conversation lives on the tablet rather than only in Discord
#
# All transport is OTA via the ddvk `rmapi` fork. No USB tether required.
#
# Pipeline summary (read):
#   1. `rmapi get <notebook>` → downloads .rmdoc bundle
#   2. Unzip into a temp dir
#   3. Identify target page from .content's `cPages.pages` array
#   4. `rmc -t svg <page>.rm` → SVG, then ImageMagick → PNG
#   5. Returned PNG can be passed to vision-OCR for handwriting recognition
#
# Pipeline summary (write):
#   1. `rmapi get <notebook>` → downloads .rmdoc bundle
#   2. Unzip
#   3. Use rmscene.simple_text_document(<text>) to compose a new typed-text
#      .rm page blob
#   4. Append page entry to .content's `cPages.pages` (idx auto-incremented
#      from the last existing page's idx)
#   5. Re-zip bundle (preserves the original UUID structure)
#   6. `rmapi put --force` → replaces the notebook on cloud, tablet syncs
#
# Concurrency: per-notebook mkdir-lock prevents concurrent read+write +
# write+write conflicts on the same notebook. Reads are still serialized
# against writes to avoid downloading mid-upload.
#
# Limitations called out so future readers don't burn time:
#   - The `--force` upload path RECREATES the cloud document. The user-
#     visible page content is preserved (we re-zip the original .rm files
#     verbatim) but the document's history / sync UUID may rotate.
#   - Typed-text pages render as machine text on reMarkable, not as
#     handwriting. That's the intended affordance for ${ASSISTANT_NAME}-side responses —
#     Sean's handwriting stays as is.
#   - Vision-OCR is OUT of this script's scope. Caller runs the OCR step
#     against the PNG output (Claude vision, openai vision, tesseract for
#     printed text, etc.).
#
# Usage:
#   <assistant>-collab.sh read <notebook> [<page>]
#     <page> defaults to last. Outputs a PNG to /tmp/<assistant>-collab-<slug>-pN.png
#     and prints its path.
#
#   <assistant>-collab.sh write <notebook> <text>
#     Appends a new typed-text page with <text>. Re-uploads via --force.
#
#   <assistant>-collab.sh count <notebook>
#     Just prints the page count. Cheap (downloads only the .content json,
#     not the full bundle — well, currently does both because rmapi doesn't
#     support partial get; future optimization).
#
# Exit codes:
#   0 = success
#   1 = bad args
#   2 = rmapi not installed / not authed
#   3 = notebook not found on tablet
#   4 = lock contention (another invocation in flight)
#   5 = parse / compose error (corrupt bundle, rmscene crash, etc.)
#   6 = upload failed (network, API, force-overwrite refused, etc.)

set -uo pipefail

RMAPI="${RMAPI_BIN:-$(command -v rmapi 2>/dev/null || echo rmapi)}"
RMC="${RMC_BIN:-$(command -v rmc 2>/dev/null || echo rmc)}"
MAGICK="${MAGICK_BIN:-$(command -v magick 2>/dev/null || echo magick)}"
PY="${PY_BIN:-$(command -v python3 2>/dev/null || echo python3)}"
TMP_ROOT="${TMP_ROOT:-/tmp/<assistant>-collab}"

usage() {
    sed -n '/^# Usage:/,/^# Exit codes:/p' "$0" | sed -E 's/^# ?//; s/^Exit codes:$//'
    exit 1
}

slugify() {
    printf '%s' "$1" | tr '[:upper:] ' '[:lower:]-' | tr -cd '[:alnum:]-_'
}

acquire_lock() {
    local notebook="$1"
    local slug
    slug=$(slugify "$notebook")
    local lock_dir="$TMP_ROOT/lock.d.$slug"
    if mkdir "$lock_dir" 2>/dev/null; then
        echo "$$" > "$lock_dir/pid"
        echo "$lock_dir"
        return 0
    fi
    local owner
    owner=$(cat "$lock_dir/pid" 2>/dev/null || echo "?")
    if [[ -n "$owner" ]] && kill -0 "$owner" 2>/dev/null; then
        echo "ERR: lock held by pid=$owner ($notebook). Try again in a moment." >&2
        return 1
    fi
    # Stale — steal
    echo "$$" > "$lock_dir/pid"
    echo "$lock_dir"
    return 0
}

release_lock() {
    local lock_dir="$1"
    [[ -n "$lock_dir" && -d "$lock_dir" ]] && rm -rf "$lock_dir"
}

ensure_tools() {
    if [[ ! -x "$RMAPI" ]]; then
        echo "ERR: rmapi not found at $RMAPI" >&2
        return 2
    fi
    if ! "$RMAPI" ls / >/dev/null 2>&1; then
        echo "ERR: rmapi auth broken. Re-pair via https://my.remarkable.com/device/desktop/connect" >&2
        return 2
    fi
    return 0
}

download_bundle() {
    local notebook="$1"
    local workdir="$2"
    cd "$workdir"
    if ! "$RMAPI" get "$notebook" >&2; then
        echo "ERR: rmapi get failed for $notebook" >&2
        return 3
    fi
    # The download lands as "<name>.rmdoc" OR "<name>.zip" depending on doc type.
    local archive
    archive=$(ls "$workdir"/*.rmdoc "$workdir"/*.zip 2>/dev/null | head -1)
    if [[ -z "$archive" ]]; then
        echo "ERR: no archive produced by rmapi get" >&2
        return 3
    fi
    if ! unzip -q "$archive" -d "$workdir/extracted"; then
        echo "ERR: unzip failed on $archive" >&2
        return 3
    fi
    echo "$archive"
    return 0
}

find_content_json() {
    local dir="$1"
    # The .content file lives at the top level of the extracted bundle.
    local content
    content=$(ls "$dir"/*.content 2>/dev/null | head -1)
    if [[ -z "$content" ]]; then
        echo "ERR: no .content file in bundle" >&2
        return 5
    fi
    echo "$content"
    return 0
}

cmd_count() {
    local notebook="$1"
    local lock_dir
    lock_dir=$(acquire_lock "$notebook") || return 4
    trap "release_lock '$lock_dir'" EXIT
    local workdir="$TMP_ROOT/work.$$"
    mkdir -p "$workdir"
    download_bundle "$notebook" "$workdir" >/dev/null || return $?
    local content
    content=$(find_content_json "$workdir/extracted") || return $?
    "$PY" -c "import json,sys; print(len(json.load(open('$content'))['cPages']['pages']))"
}

cmd_read() {
    local notebook="$1"
    local page_arg="${2:-last}"
    local lock_dir
    lock_dir=$(acquire_lock "$notebook") || return 4
    trap "release_lock '$lock_dir'" EXIT
    local workdir="$TMP_ROOT/work.$$"
    mkdir -p "$workdir"
    download_bundle "$notebook" "$workdir" >/dev/null || return $?
    local content
    content=$(find_content_json "$workdir/extracted") || return $?

    # Resolve page index → .rm file id
    local page_id
    page_id=$("$PY" - "$content" "$page_arg" <<'PY'
import json, sys
content_path, page_arg = sys.argv[1], sys.argv[2]
pages = json.load(open(content_path))['cPages']['pages']
if page_arg in ('last', 'end', '-1'):
    idx = len(pages) - 1
else:
    idx = int(page_arg) - 1
if idx < 0 or idx >= len(pages):
    sys.stderr.write(f"page out of range (1..{len(pages)})\n")
    sys.exit(1)
print(pages[idx]['id'])
PY
    )
    if [[ -z "$page_id" ]]; then
        return 1
    fi

    # The .rm lives under <bundle-uuid>/<page-id>.rm. Some pages in
    # content.cPages.pages may be ghost entries (e.g. typed-text pages
    # whose .rm wasn't preserved by `rmapi put --force` on a prior write
    # cycle). Fall back to the latest concrete .rm file on disk when the
    # requested page's .rm is missing — gives a useful "what's actually
    # visible on the tablet" answer instead of erroring out.
    local rm_file
    rm_file=$(find "$workdir/extracted" -name "$page_id.rm" | head -1)
    if [[ -z "$rm_file" || ! -f "$rm_file" ]]; then
        if [[ "$page_arg" == "last" || "$page_arg" == "end" || "$page_arg" == "-1" ]]; then
            rm_file=$(find "$workdir/extracted" -name "*.rm" | xargs ls -t 2>/dev/null | head -1)
            if [[ -n "$rm_file" && -f "$rm_file" ]]; then
                echo "WARN: page $page_id .rm missing — falling back to latest .rm by mtime: $(basename "$rm_file")" >&2
            fi
        fi
    fi
    if [[ -z "$rm_file" || ! -f "$rm_file" ]]; then
        echo "ERR: page .rm file not found: $page_id" >&2
        return 5
    fi

    local slug
    slug=$(slugify "$notebook")
    local svg_out="$TMP_ROOT/$slug-$page_id.svg"
    local png_out="$TMP_ROOT/$slug-$page_id.png"
    if ! "$RMC" -t svg -o "$svg_out" "$rm_file" 2>&2; then
        echo "ERR: rmc svg conversion failed" >&2
        return 5
    fi
    if ! "$MAGICK" "$svg_out" "$png_out" 2>&2; then
        echo "ERR: imagemagick svg→png failed" >&2
        return 5
    fi
    echo "$png_out"
    return 0
}

cmd_write() {
    local notebook="$1"
    local text="$2"
    if [[ -z "$text" ]]; then
        echo "ERR: write requires non-empty text" >&2
        return 1
    fi
    local lock_dir
    lock_dir=$(acquire_lock "$notebook") || return 4
    trap "release_lock '$lock_dir'" EXIT
    local workdir="$TMP_ROOT/work.$$"
    mkdir -p "$workdir"
    local archive
    archive=$(download_bundle "$notebook" "$workdir") || return $?
    local content
    content=$(find_content_json "$workdir/extracted") || return $?

    # Compose the new page + patch content json + re-zip
    "$PY" - "$workdir/extracted" "$content" "$text" <<'PY' >&2
import json, pathlib, sys, time, uuid
import rmscene
from rmscene import simple_text_document, write_blocks

extracted_root = pathlib.Path(sys.argv[1])
content_path = pathlib.Path(sys.argv[2])
text = sys.argv[3]

content = json.loads(content_path.read_text())
pages = content['cPages']['pages']

# The bundle dir holding per-page .rm files is named <bundle-uuid> at the
# extracted root.
bundle_uuid = content_path.stem
bundle_dir = extracted_root / bundle_uuid
if not bundle_dir.is_dir():
    sys.exit(f"bundle dir {bundle_dir} missing")

new_id = str(uuid.uuid4())
last_idx = pages[-1]['idx']['value']
# idx values are short base-26-ish strings; increment the last char.
new_idx = last_idx[:-1] + chr(ord(last_idx[-1]) + 1)
ts_ms = int(time.time() * 1000)
last_template = pages[-1].get('template', {'timestamp': '3:2', 'value': 'Blank'})

# Write the new .rm
new_rm = bundle_dir / f"{new_id}.rm"
with new_rm.open('wb') as f:
    blocks = list(simple_text_document(text))
    write_blocks(f, blocks)

# Append to content.cPages.pages
pages.append({
    'id': new_id,
    'idx': {'timestamp': '3:2', 'value': new_idx},
    'modifed': str(ts_ms),
    'template': last_template,
})

# Persist content
content_path.write_text(json.dumps(content, separators=(',', ':')))
print(f"appended new page id={new_id} idx={new_idx} text_len={len(text)}", file=sys.stderr)
PY
    local py_rc=$?
    if [[ $py_rc -ne 0 ]]; then
        echo "ERR: python compose step failed (rc=$py_rc)" >&2
        return 5
    fi

    # Re-zip the bundle, preserving the original archive's STORED compression
    # method. rmapi expects an .rmdoc bundle.
    local rezip="$workdir/repacked.rmdoc"
    (cd "$workdir/extracted" && zip -q -0 -r "$rezip" .) || {
        echo "ERR: re-zip failed" >&2
        return 5
    }

    # Upload back with --force (recreates the document — preserves user-
    # facing name + page content but rotates the sync UUID).
    if ! "$RMAPI" put --force "$rezip" / >&2; then
        echo "ERR: rmapi put --force failed" >&2
        return 6
    fi

    echo "OK: appended page to '$notebook'"
    return 0
}

# --- main ---
[[ $# -lt 2 ]] && usage
mkdir -p "$TMP_ROOT"

verb="$1"; shift
case "$verb" in
    count)
        ensure_tools || exit $?
        cmd_count "$1"
        ;;
    read)
        ensure_tools || exit $?
        cmd_read "$1" "${2:-last}"
        ;;
    write)
        ensure_tools || exit $?
        notebook="$1"; shift
        cmd_write "$notebook" "$*"
        ;;
    *)
        usage
        ;;
esac
