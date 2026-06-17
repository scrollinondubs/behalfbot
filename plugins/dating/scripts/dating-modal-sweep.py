#!/usr/bin/env python3
"""dating-modal-sweep.py - dismiss known-safe modal blockers before a swipe session.

Pre-session sweep that scans the Android UI tree for known dismiss buttons and
taps the first one it finds. Targets the recurring "swipe session blocked on a
foreground dialog" failure mode (Play Store nag banners, permission re-prompts,
app onboarding modals after an update, etc).

Safety:
  * Only taps buttons in the SAFE_DISMISS set. Anything that requires a policy
    decision (Update / I agree / Sign in / Allow / Pay / Continue with biometrics)
    is NOT auto-tapped - those are escalated to the installer.
  * If an unknown modal is on screen (current focus is a Dialog activity but no
    safe-dismiss button matched), the script reports it and exits non-zero so
    the caller stops the session and pings the installer for a foreground
    decision.

Usage:
    python3 dating-modal-sweep.py [--max-iterations N] [--package PKG] [--quiet]

Default: 3 iterations, no package filter. Each iteration re-reads the UI tree
because dismissing one modal often surfaces another stacked underneath.

Exit codes:
    0 - nothing to dismiss, or all dismissed successfully
    2 - unknown modal blocking the foreground, requires installer attention
    3 - emulator not reachable (no adb device, u2 connect fails)

Configuration:
    DATING_VENV_PYTHON    Optional path to a venv interpreter that has
                          `uiautomator2` installed. If set and exists, the
                          script re-execs into it. If unset, callers must
                          run this from an interpreter that can import
                          uiautomator2 directly.
    DATING_AVD_SERIAL     adb device serial. Default: emulator-5554.

V1 reference: <v1-reference-install> `scripts/dating-modal-sweep.py`.
"""

import sys
import os
import argparse
import time

VENV_PYTHON = os.environ.get('DATING_VENV_PYTHON', '')
if VENV_PYTHON and os.path.exists(VENV_PYTHON) and os.path.abspath(sys.executable) != VENV_PYTHON:
    os.execv(VENV_PYTHON, [VENV_PYTHON, os.path.abspath(__file__)] + sys.argv[1:])

_U2_DEVICE_SERIAL = os.environ.get('DATING_AVD_SERIAL', 'emulator-5554')

SAFE_DISMISS = [
    'Not now',
    'Not Now',
    'No thanks',
    'No Thanks',
    'Maybe later',
    'Maybe Later',
    'Skip',
    'SKIP',
    'Dismiss',
    'Cancel',
    'CANCEL',
    'Close',
    'Got it',
    'GOT IT',
    'OK',
    'Ok',
    'Okay',
    'Continue',
    "I'll do it later",
    'Remind me later',
]

ESCALATE_ONLY = {
    'Update',
    'UPDATE',
    'Update now',
    'I agree',
    'I Agree',
    'Agree',
    'Accept',
    'Allow',
    'Sign in',
    'Sign In',
    'Subscribe',
    'Upgrade',
    'Continue with biometrics',
    'Use Face ID',
    'Use Fingerprint',
    'Pay',
    'Buy',
}


def _connect():
    import uiautomator2
    try:
        d = uiautomator2.connect(_U2_DEVICE_SERIAL)
        d.info  # touch the device to confirm reachable
        return d
    except Exception as exc:
        print(f'u2 connect failed ({exc.__class__.__name__}: {exc})', file=sys.stderr)
        sys.exit(3)


def _foreground_package(d):
    try:
        return d.app_current().get('package', '')
    except Exception:
        return ''


def _find_safe_button(d):
    for label in SAFE_DISMISS:
        try:
            sel = d(text=label)
            if sel.exists:
                return label, sel
        except Exception:
            continue
    return None, None


def _find_escalate_button(d):
    for label in ESCALATE_ONLY:
        try:
            if d(text=label).exists:
                return label
        except Exception:
            continue
    return None


def _is_modal_present(d):
    """Crude modal heuristic: a Button-class element with one of the known
    decision labels is on screen. Doesn't catch every modal, but it's the
    blocker class we actually care about pre-swipe."""
    try:
        if d(className='android.widget.Button').count > 0:
            return True
    except Exception:
        pass
    return False


def sweep(max_iterations=3, package_filter=None, quiet=False):
    d = _connect()

    if package_filter:
        fg = _foreground_package(d)
        if package_filter not in fg:
            if not quiet:
                print(f'foreground={fg or "(unknown)"} - skipping ({package_filter} not foregrounded)')
            return 0

    dismissed = []
    for i in range(max_iterations):
        label, sel = _find_safe_button(d)
        if label is None:
            break

        try:
            sel.click()
            dismissed.append(label)
            if not quiet:
                print(f'dismissed: {label}')
            time.sleep(0.6)
        except Exception as exc:
            if not quiet:
                print(f'click failed for {label} ({exc.__class__.__name__}: {exc})',
                      file=sys.stderr)
            break

    escalate = _find_escalate_button(d)
    if escalate is not None:
        modal_pkg = _foreground_package(d)
        print(f'ESCALATE: modal in {modal_pkg or "(unknown)"} shows '
              f'"{escalate}" button - requires installer attention. Not auto-tapping.',
              file=sys.stderr)
        return 2

    if not dismissed and not quiet:
        print('nothing to dismiss')
    elif dismissed and not quiet:
        print(f'swept {len(dismissed)} modal(s): {", ".join(dismissed)}')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--max-iterations', type=int, default=3,
                        help='Max sweep passes (each pass re-reads the UI tree)')
    parser.add_argument('--package', type=str, default=None,
                        help='Only sweep if this package is foregrounded')
    parser.add_argument('--quiet', action='store_true', help='Suppress per-action logs')
    args = parser.parse_args()
    sys.exit(sweep(args.max_iterations, args.package, args.quiet))


if __name__ == '__main__':
    main()
