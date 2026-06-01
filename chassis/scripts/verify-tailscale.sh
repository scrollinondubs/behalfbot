#!/usr/bin/env bash
# verify-tailscale.sh - pre-kickoff sanity check for Tailscale + SSH key wiring.
#
# Run this on the installer's host box, as the agent user, BEFORE pinging Sean
# to schedule the install kickoff session. All 7 checks must be green.
#
# Usage:
#   bash chassis/scripts/verify-tailscale.sh
#
# Environment (optional overrides):
#   TAILSCALE_CANARY_HOST   Tailscale FQDN of a Sean+${ASSISTANT_NAME} node to ping as a
#                           connectivity proof. Defaults to jaxs-mac-mini.tail20bf90.ts.net.
#   SEAN_SSH_PUBKEY         Full public key string for Sean's primary key.
#                           Defaults to the hardcoded canonical key below.
#                           Set this env var if Sean has rotated his key.
#
# Exit codes:
#   0 - all checks passed (green to ping Sean)
#   1 - one or more checks failed (fix red items above before pinging Sean)
#
# Part of issue scrollinondubs/behalfbot-chassis#53 item 5.

set -uo pipefail

# ============================================================
# Config
# ============================================================

# ${ASSISTANT_NAME}'s SSH public key (Mac Mini M4, Lisbon - <v1-reference-install>@vibecodelisboa.com)
CHASSIS_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFEmdRe62zAQ2Ot/IpKd8omBb5+RLUU5BuvV7+CuKPIj <v1-reference-install>@vibecodelisboa.com"
CHASSIS_PUBKEY_COMMENT="<v1-reference-install>@vibecodelisboa.com"

# Sean's SSH public key (primary - sean@grid7.com)
# Installer: if Sean has provided a different key, set SEAN_SSH_PUBKEY in env.
SEAN_PUBKEY_DEFAULT="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGLsG6EhJL2Cwk+LkoaIi4xElXM3f/vGhi2+VlxlhT3Z sean@grid7.com"
SEAN_PUBKEY="${SEAN_SSH_PUBKEY:-$SEAN_PUBKEY_DEFAULT}"
SEAN_PUBKEY_COMMENT="sean@grid7.com"

# Tailscale connectivity canary: a node on Sean+${ASSISTANT_NAME}'s tailnet
CANARY_HOST="${TAILSCALE_CANARY_HOST:-jaxs-mac-mini.tail20bf90.ts.net}"

# ============================================================
# Output helpers
# ============================================================

# Color support: suppress if not a terminal or if NO_COLOR is set
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  GREEN="\033[0;32m"
  RED="\033[0;31m"
  YELLOW="\033[0;33m"
  BOLD="\033[1m"
  RESET="\033[0m"
else
  GREEN="" RED="" YELLOW="" BOLD="" RESET=""
fi

PASS_COUNT=0
FAIL_COUNT=0

pass() {
  local label="$1"
  PASS_COUNT=$(( PASS_COUNT + 1 ))
  printf "  ${GREEN}[PASS]${RESET} %s\n" "$label"
}

fail() {
  local label="$1"
  local fix="$2"
  FAIL_COUNT=$(( FAIL_COUNT + 1 ))
  printf "  ${RED}[FAIL]${RESET} %s\n" "$label"
  printf "        ${YELLOW}Fix:${RESET} %s\n" "$fix"
}

warn() {
  printf "  ${YELLOW}[WARN]${RESET} %s\n" "$1"
}

section() {
  printf "\n${BOLD}%s${RESET}\n" "$1"
}

# ============================================================
# Check 1 - Tailscale installed
# ============================================================

section "1/7  Tailscale installed"

if ! command -v tailscale >/dev/null 2>&1; then
  fail "tailscale binary not found" \
    "Install Tailscale: curl -fsSL https://tailscale.com/install.sh | sh"
  # Cannot run any further Tailscale checks - emit remaining skips and exit early
  printf "\n%sCannot continue: Tailscale is not installed. Install it and re-run.%s\n" "$RED" "$RESET"
  exit 1
else
  pass "tailscale binary found at $(command -v tailscale)"
fi

# ============================================================
# Check 2 - Tailscale daemon running + logged in
# ============================================================

section "2/7  Tailscale running and logged in"

ts_json=""
ts_json=$(tailscale status --json 2>/dev/null) || true

if [[ -z "$ts_json" ]]; then
  fail "tailscale status returned no output - daemon may not be running" \
    "Start the daemon: sudo systemctl enable --now tailscaled  (Linux) or  sudo launchctl load /Library/LaunchDaemons/com.tailscale.tailscaled.plist  (macOS)"
else
  backend_state=$(printf '%s' "$ts_json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('BackendState',''))" 2>/dev/null || echo "")
  have_key=$(printf '%s' "$ts_json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('HaveNodeKey', False))" 2>/dev/null || echo "False")

  if [[ "$backend_state" == "Running" && "$have_key" == "True" ]]; then
    self_dns=$(printf '%s' "$ts_json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Self',{}).get('DNSName',''))" 2>/dev/null | sed 's/\.$//') || self_dns=""
    pass "Tailscale running (BackendState=Running, HaveNodeKey=True)"
    if [[ -n "$self_dns" ]]; then
      printf "        Node FQDN: %s\n" "$self_dns"
      printf "        Share this FQDN with Sean so he can SSH: ssh <agent-user>@%s\n" "$self_dns"
    fi
  elif [[ "$backend_state" == "NeedsLogin" || "$have_key" == "False" ]]; then
    fail "Tailscale is installed but not logged in (BackendState=$backend_state)" \
      "Run: sudo tailscale up    then authenticate in the browser"
  else
    fail "Tailscale daemon state unexpected: BackendState=$backend_state" \
      "Run: sudo systemctl status tailscaled    and check logs for errors"
  fi
fi

# ============================================================
# Check 3 - Node share accepted into Sean+${ASSISTANT_NAME}'s tailnet
# ============================================================

section "3/7  Node visible on Sean+${ASSISTANT_NAME} tailnet (outbound Tailscale ping)"

# The best observable signal from the installer's box: if the node share was
# accepted, Sean+${ASSISTANT_NAME}'s nodes appear as reachable peers. We ping the canary host.
# If unreachable, either the share was not sent, not accepted, or Tailscale ACLs
# block the path.
#
# Note: 'tailscale ping' exits 0 only if a response is received. We allow up to
# 5 seconds before giving up.
ping_out=""
ping_out=$(tailscale ping --c 1 --timeout 5s "$CANARY_HOST" 2>&1) || true

if printf '%s' "$ping_out" | grep -qiE "(pong|is local Tailscale)"; then
  pass "Tailscale ping to $CANARY_HOST succeeded (node share is live)"
else
  fail "Tailscale ping to $CANARY_HOST failed - share not accepted or ACL blocking" \
    "In the Tailscale admin panel (https://login.tailscale.com/admin/machines), find this node, click '...' > Share, and send the share link to sean@grid7.com. Then confirm Sean has accepted it. See docs/installer-homework.md Step 2c."
  warn "If you've already shared the node, ask Sean to confirm he accepted the invite."
  warn "If TAILSCALE_CANARY_HOST is wrong, re-run: TAILSCALE_CANARY_HOST=<correct-host> bash verify-tailscale.sh"
fi

# ============================================================
# Check 4 - Agent user authorized_keys exists with correct perms
# ============================================================

section "4/7  authorized_keys exists with correct permissions"

auth_keys="$HOME/.ssh/authorized_keys"

if [[ ! -f "$auth_keys" ]]; then
  fail "$auth_keys does not exist" \
    "Run: mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
else
  # Check ownership: file must be owned by current user
  if command -v stat >/dev/null 2>&1; then
    # stat format differs between Linux and macOS
    if stat --version 2>/dev/null | grep -q "GNU"; then
      file_owner=$(stat -c '%U' "$auth_keys" 2>/dev/null || echo "unknown")
      file_perms=$(stat -c '%a' "$auth_keys" 2>/dev/null || echo "unknown")
    else
      # macOS stat
      file_owner=$(stat -f '%Su' "$auth_keys" 2>/dev/null || echo "unknown")
      file_perms=$(stat -f '%Lp' "$auth_keys" 2>/dev/null || echo "unknown")
    fi
    current_user=$(id -un)

    if [[ "$file_owner" != "$current_user" ]]; then
      fail "$auth_keys is owned by $file_owner, expected $current_user" \
        "Run: chown $current_user:$current_user ~/.ssh/authorized_keys"
    elif [[ "$file_perms" != "600" ]]; then
      fail "$auth_keys permissions are $file_perms, expected 600" \
        "Run: chmod 600 ~/.ssh/authorized_keys"
    else
      pass "$auth_keys exists, owned by $current_user, perms 600"
    fi
  else
    pass "$auth_keys exists (could not verify perms - stat unavailable)"
    warn "Manually confirm: ls -la ~/.ssh/authorized_keys should show -rw------- owned by you"
  fi
fi

# ============================================================
# Check 5 - ${ASSISTANT_NAME}'s public key is present
# ============================================================

section "5/7  ${ASSISTANT_NAME}'s SSH public key in authorized_keys"

if [[ ! -f "$auth_keys" ]]; then
  fail "authorized_keys missing - skipping key check" \
    "Fix Check 4 first, then add ${ASSISTANT_NAME}'s key."
else
  # Match on the key blob (second field) to be robust against comment differences
  jax_key_blob=$(printf '%s' "$CHASSIS_PUBKEY" | awk '{print $2}')

  if grep -qF "$jax_key_blob" "$auth_keys" 2>/dev/null; then
    pass "${ASSISTANT_NAME}'s public key present ($CHASSIS_PUBKEY_COMMENT)"
  else
    fail "${ASSISTANT_NAME}'s public key NOT found in $auth_keys" \
      "Append this line to ~/.ssh/authorized_keys:
        $CHASSIS_PUBKEY"
  fi
fi

# ============================================================
# Check 6 - Sean's public key is present
# ============================================================

section "6/7  Sean's SSH public key in authorized_keys"

if [[ ! -f "$auth_keys" ]]; then
  fail "authorized_keys missing - skipping key check" \
    "Fix Check 4 first, then add Sean's key."
else
  sean_key_blob=$(printf '%s' "$SEAN_PUBKEY" | awk '{print $2}')

  if grep -qF "$sean_key_blob" "$auth_keys" 2>/dev/null; then
    pass "Sean's public key present ($SEAN_PUBKEY_COMMENT)"
  else
    fail "Sean's public key NOT found in $auth_keys" \
      "Append this line to ~/.ssh/authorized_keys:
        $SEAN_PUBKEY
        (If Sean provided a different key, set: export SEAN_SSH_PUBKEY='<key>' and re-run)"
  fi
fi

# ============================================================
# Check 7 - SSH daemon running
# ============================================================

section "7/7  SSH daemon running"

sshd_ok=false

if command -v systemctl >/dev/null 2>&1; then
  # Linux: check systemd
  if systemctl is-active --quiet sshd 2>/dev/null || systemctl is-active --quiet ssh 2>/dev/null; then
    sshd_ok=true
    pass "sshd is active (systemd)"
  fi
elif command -v launchctl >/dev/null 2>&1; then
  # macOS: check launchd
  if launchctl list com.openssh.sshd >/dev/null 2>&1; then
    sshd_ok=true
    pass "sshd is active (launchd)"
  fi
fi

if [[ "$sshd_ok" == "false" ]]; then
  # Final fallback: check if something is listening on port 22
  if command -v ss >/dev/null 2>&1; then
    if ss -tlnp 2>/dev/null | grep -q ':22 '; then
      sshd_ok=true
      pass "Something is listening on port 22 (sshd assumed running)"
    fi
  elif command -v netstat >/dev/null 2>&1; then
    if netstat -tlnp 2>/dev/null | grep -q ':22 '; then
      sshd_ok=true
      pass "Something is listening on port 22 (sshd assumed running)"
    fi
  fi
fi

if [[ "$sshd_ok" == "false" ]]; then
  fail "SSH daemon does not appear to be running" \
    "Install + start: sudo apt install openssh-server && sudo systemctl enable --now sshd
        Then confirm: sudo systemctl status sshd"
fi

# ============================================================
# Summary
# ============================================================

TOTAL=$(( PASS_COUNT + FAIL_COUNT ))
printf "\n${BOLD}Tailscale verify: %d/%d checks passed" "$PASS_COUNT" "$TOTAL"

if [[ "$FAIL_COUNT" -eq 0 ]]; then
  printf " - all green.%s\n" "$RESET"
  printf "%sReady to ping Sean. Include your Tailscale FQDN in the message.%s\n\n" "$GREEN" "$RESET"
  exit 0
else
  printf " - fix %d red item(s) above before pinging Sean.${RESET}\n\n" "$FAIL_COUNT"
  exit 1
fi
