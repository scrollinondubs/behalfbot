#!/usr/bin/env bash
# .github/scripts/check-shell-portability.sh
# ==========================================
# Advisory grep for shell constructs that work on exactly one of the two
# platforms chassis code runs on.
#
# The chassis runs in two places and shell-tests.yml now proves both: a Debian
# container (bash 5.x, GNU coreutils) and the operator's macOS host, where
# /bin/bash is 3.2.57 (Apple has shipped 3.2 since 2007 for licensing reasons)
# with a BSD userland. Those behavioural suites are the real coverage. This
# check exists for the code they do not reach - hooks, gather scripts, one-shot
# migration helpers, the dispatcher - where nobody is going to write a suite
# and the failure mode is silence rather than a crash.
#
# Three bugs shipped green through the ubuntu-only lane on 2026-09-05:
#
#   1. ${BAKED_ROOTS[-1]:-} in chassis/scripts/resolve-plugin-root.sh.
#      Negative array subscripts need bash 4.3. On 3.2 that is a `bad array
#      subscript` error that expands to empty, so the resolver handed callers
#      an EMPTY plugin root on the compose-failure path. Fixed in #197.
#   2. `stat -f %m` in the colima work. A filesystem query on GNU coreutils, an
#      mtime query on BSD. Chained with `||` the two outputs concatenated and
#      silently disabled a recycled-pid lock breaker. Fixed in #193.
#   3. `date -j -f` in the conservation-mode auto-lift. BSD only, so inside the
#      Debian container lift_epoch became 0, `now >= 0` was always true, and
#      any flag with an auto_lift_after was lifted on the very next tick while
#      logging the same line a working auto-lift logs. Fixed in #195.
#
# Note the direction of 3: BSD-only code breaking on Linux. This is not a
# "macOS support" check, it is a "the code must survive both" check, so every
# dialect rule below is symmetric.
#
# ADVISORY. Exits 0 with warnings by default so it can never block a PR on a
# heuristic. `--strict` exits 1 on any finding, which is what the self-test
# uses. Findings are emitted as GitHub `::warning file=,line=::` annotations so
# they land on the PR's Files tab.
#
# Escape hatch: put `portable-ok: <reason>` anywhere on the offending line.
# Use it for code that is deliberately single-platform (a macOS-only colima
# wrapper, a Linux-only entrypoint) and say which platform and why.
#
# Dialect pairs are matched over a 7-line window (3 above, 3 below) so the
# ordinary two-line "try one dialect, fall back to the other" idiom does not
# warn. For `stat` the ORDER matters and the window checks it: GNU
# `stat -f %m FILE` prints a filesystem block to STDOUT and exits 1, so a
# BSD-first chain pollutes the output of the GNU fallback that follows it.
# GNU-first is the only safe order.
#
# Usage:
#   check-shell-portability.sh [--strict] [PATH ...]

set -uo pipefail

STRICT=0
PATHS=()
for arg in "$@"; do
    case "$arg" in
        --strict) STRICT=1 ;;
        -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
        *) PATHS+=("$arg") ;;
    esac
done
[[ ${#PATHS[@]} -eq 0 ]] && PATHS=(".")

# Self-exclusion. This script and its self-test both contain the offending
# idioms as literal regex source and fixture text, so scanning them would
# produce nothing but noise about itself.
SELF_BASE="check-shell-portability.sh"
TEST_BASE="test-check-shell-portability.sh"

# Shell files: *.sh, plus any extensionless file whose FIRST line is a bash or
# zsh shebang. First line only - grepping the whole file matches fenced code
# blocks inside markdown docs. The zsh dispatcher is in scope deliberately: the
# bash-4 rules do not apply to it but every BSD/GNU rule does, and bug 3's
# idiom still lives in heartbeat-dispatcher.sh.
CANDIDATES=$(
    find "${PATHS[@]}" \
        -type d \( -name .git -o -name node_modules -o -name .venv -o -name vendor \) -prune -o \
        -type f -print 2>/dev/null
)

FILES=""
while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    case "$(basename "$f")" in
        "$SELF_BASE"|"$TEST_BASE") continue ;;
    esac
    case "$f" in
        *.sh) ;;
        *)
            head -1 "$f" 2>/dev/null | grep -qE '^#!.*(bash|zsh)' || continue
            ;;
    esac
    FILES="$FILES$f
"
done <<< "$CANDIDATES"

if [[ -z "${FILES//[[:space:]]/}" ]]; then
    echo "check-shell-portability: no shell files under ${PATHS[*]}"
    exit 0
fi

read -r -d '' AWK_PROG <<'AWKEOF' || true
function warn(file, ln, klass, title, msg, text) {
    printf "::warning file=%s,line=%d,title=%s::%s\n", file, ln, title, msg
    printf "  %s:%d [%s/%s] %s\n", file, ln, klass, title, msg
    printf "      %s\n", text
    hits++
}
function window(i,   lo, hi, j, w) {
    lo = i - 3; if (lo < 1) lo = 1
    hi = i + 3; if (hi > n) hi = n
    w = ""
    for (j = lo; j <= hi; j++) w = w L[j] "\n"
    return w
}
function pos(s, re) { return match(s, re) ? RSTART : 0 }

BEGIN {
    hits = 0
    BSD_DATE = "(^|[^A-Za-z0-9_-])date[ \t]+(-[A-Za-z][^ \t]*[ \t]+)*-[jvr]"
    GNU_DATE = "(^|[^A-Za-z0-9_-])date[ \t]+(-[A-Za-z][^ \t]*[ \t]+)*-d([ \t]|$)"
    BSD_STAT = "stat[ \t]+(-[A-Za-z][^ \t]*[ \t]+)*-[A-Za-z]*f"
    GNU_STAT = "stat[ \t]+(-[A-Za-z][^ \t]*[ \t]+)*-[A-Za-z]*c"
}

{ L[NR] = $0 }

END {
    n = NR
    for (i = 1; i <= n; i++) {
        line = L[i]
        stripped = line
        sub(/^[ \t]+/, "", stripped)
        if (stripped ~ /^#/) continue          # comment-only line
        if (line ~ /portable-ok:/) continue    # explicit escape hatch
        if (line ~ /^[ \t]*$/) continue

        # ---------- bash 4 only. Fatal on the macOS host (/bin/bash 3.2).
        if (!IS_ZSH) {
            if (line ~ /\$\{[A-Za-z_][A-Za-z0-9_]*\[-[0-9]+\]/)
                warn(FILENAME, i, "bash4", "negative-array-subscript", "negative array subscript needs bash 4.3; on macOS bash 3.2 it errors and expands to EMPTY (bug 1, behalfbot#197)", line)
            if (line ~ /(^|[^A-Za-z0-9_-])(declare|local|typeset)[ \t]+(-[A-Za-z]+[ \t]+)*-[A-Za-z]*A/)
                warn(FILENAME, i, "bash4", "associative-array", "associative arrays need bash 4.0; macOS /bin/bash is 3.2", line)
            if (line ~ /\$\{[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?(\^\^|,,|\^|,)[^}]*\}/)
                warn(FILENAME, i, "bash4", "case-modification", "${var^^} / ${var,,} case modification needs bash 4.0; macOS /bin/bash is 3.2", line)
            if (line ~ /(^|[ \t;&|(])(mapfile|readarray)[ \t]/)
                warn(FILENAME, i, "bash4", "mapfile", "mapfile / readarray need bash 4.0; on macOS bash 3.2 the target array is left EMPTY", line)
            if (line ~ /(^|[^A-Za-z0-9_-])(declare|local|typeset)[ \t]+(-[A-Za-z]+[ \t]+)*-[A-Za-z]*n([ \t]|$)/)
                warn(FILENAME, i, "bash4", "nameref", "declare -n namerefs need bash 4.3; macOS /bin/bash is 3.2", line)
            if (line ~ /&>>/)
                warn(FILENAME, i, "bash4", "append-both-streams", "&>> needs bash 4.0; use >>FILE 2>&1", line)
            if (line ~ /;;&/)
                warn(FILENAME, i, "bash4", "case-fallthrough", ";;& case fallthrough needs bash 4.0", line)
            if (line ~ /(^|[ \t;&|(])coproc[ \t]/)
                warn(FILENAME, i, "bash4", "coproc", "coproc needs bash 4.0", line)
            if (line ~ /(^|[ \t;&|(])wait[ \t]+-[A-Za-z]*n([ \t]|$)/)
                warn(FILENAME, i, "bash4", "wait-n", "wait -n needs bash 4.3", line)
            if (line ~ /shopt[ \t]+-s[ \t]+globstar/)
                warn(FILENAME, i, "bash4", "globstar", "globstar needs bash 4.0", line)
            if (line ~ /\$\{[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?@[A-Za-z]\}/)
                warn(FILENAME, i, "bash4", "parameter-transform", "${var@Q} parameter transformation needs bash 4.4", line)
        }

        # ---------- BSD vs GNU. Fatal on whichever platform the author is not on.
        w = window(i)

        if (line ~ BSD_STAT) {
            pf = pos(w, BSD_STAT)
            pc = pos(w, GNU_STAT)
            if (pc == 0)
                warn(FILENAME, i, "dialect", "stat-bsd-only", "BSD `stat -f` with no GNU `stat -c` fallback nearby; wrong answer inside the Debian container", line)
            else if (pc > pf)
                warn(FILENAME, i, "dialect", "stat-wrong-order", "BSD `stat -f` runs BEFORE the GNU `stat -c` fallback. On Linux `stat -f` still writes a filesystem block to stdout, so the two outputs concatenate (bug 2, behalfbot#193). Put `stat -c` first", line)
        }

        has_bsd_date = (line ~ BSD_DATE)
        has_gnu_date = (line ~ GNU_DATE)
        if (has_bsd_date && !has_gnu_date && !(w ~ GNU_DATE))
            warn(FILENAME, i, "dialect", "date-bsd-only", "BSD `date -j`/`-v`/`-r` with no GNU `date -d` fallback nearby; inside the Debian container this takes the error branch and every timestamp becomes epoch 0 (bug 3, behalfbot#195)", line)
        if (has_gnu_date && !has_bsd_date && !(w ~ BSD_DATE))
            warn(FILENAME, i, "dialect", "date-gnu-only", "GNU `date -d` with no BSD `date -j`/`-v`/`-r` fallback nearby; fails on the macOS host", line)

        # GNU `sed -i` takes no argument, BSD requires one. `sed -i.bak` is the
        # only spelling that works on both.
        if (line ~ /sed[ \t]+(-[A-Za-z]+[ \t]+)*-i([ \t]|$)/)
            warn(FILENAME, i, "dialect", "sed-i", "`sed -i` takes no argument on GNU and requires one on BSD; use `sed -i.bak ... && rm -f FILE.bak` or write through a temp file", line)

        # GNU-only binaries. Suppressed by a `command -v` probe in the window,
        # which is how the repo already guards most of these.
        split("md5sum sha256sum sha1sum flock timeout", gnutools, " ")
        for (t in gnutools) {
            tool = gnutools[t]
            if (line ~ ("(^|[ \t;&|]|\\$\\()" tool "[ \t]"))
                if (!(w ~ ("command[ \t]+-v[ \t]+" tool)))
                    warn(FILENAME, i, "dialect", "gnu-only-tool", "`" tool "` is GNU coreutils and is absent on a stock macOS host; probe with `command -v` and fall back", line)
        }
        if (line ~ /grep[ \t]+(-[A-Za-z]+[ \t]+)*-[A-Za-z]*P([ \t]|$)/)
            warn(FILENAME, i, "dialect", "grep-P", "`grep -P` is GNU only; BSD grep has no PCRE mode", line)
        if (line ~ /base64[ \t]+(-[A-Za-z]+[ \t]+)*-w/)
            warn(FILENAME, i, "dialect", "base64-w", "`base64 -w` is GNU only; BSD base64 does not wrap", line)
        if (line ~ /xargs[ \t]+(-[A-Za-z]+[ \t]+)*-r([ \t]|$)/)
            warn(FILENAME, i, "dialect", "xargs-r", "`xargs -r` is GNU only; BSD xargs already skips empty input", line)
        if (line ~ /find[ \t].*-printf/)
            warn(FILENAME, i, "dialect", "find-printf", "`find -printf` is GNU only", line)
    }
    printf "##HITS %d\n", hits
}
AWKEOF

TOTAL=0
SCANNED=0
while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    SCANNED=$(( SCANNED + 1 ))
    is_zsh=0
    head -1 "$f" 2>/dev/null | grep -q 'zsh' && is_zsh=1
    out=$(awk -v IS_ZSH="$is_zsh" "$AWK_PROG" "$f" 2>/dev/null) || true
    count=$(printf '%s\n' "$out" | sed -n 's/^##HITS //p' | tail -1)
    [[ -n "$count" ]] || count=0
    if [[ "$count" -gt 0 ]]; then
        printf '%s\n' "$out" | grep -v '^##HITS '
        TOTAL=$(( TOTAL + count ))
    fi
done <<< "$FILES"

echo
echo "check-shell-portability: $TOTAL finding(s) across $SCANNED shell file(s)"
if [[ $TOTAL -gt 0 ]]; then
    echo "check-shell-portability: advisory. Silence a deliberate single-platform"
    echo "  line with a trailing '# portable-ok: <which platform, and why>'."
fi
if [[ $TOTAL -gt 0 && $STRICT -eq 1 ]]; then
    echo "check-shell-portability: --strict, failing"
    exit 1
fi
exit 0
