# Security policy

## Reporting a vulnerability

**Do not open a public issue.** A public issue discloses the problem to everyone
at the same moment, including anyone who would use it.

Use GitHub's private vulnerability reporting instead:

**[Report a vulnerability](https://github.com/scrollinondubs/behalfbot/security/advisories/new)**

It is enabled on this repo. The report is visible only to the maintainer until a
fix is ready, and it gives us a private thread to ask questions in.

Expect a first response within a few days. This is a small project with one
maintainer, so please do not read silence as dismissal - if a week passes with
nothing, feel free to nudge by opening a public issue that says only "I filed a
private report, please look", with no detail in it.

## What is in scope

This project is an always-on agent that runs with broad access on someone's own
machine, so the interesting failures are usually about **the boundary between
untrusted input and privileged action** rather than memory safety.

Particularly wanted:

- **A way around the hook layer.** `chassis/.claude/hooks/guardrails.sh` is
  deterministic enforcement that is supposed to hold even when the model is
  confused or adversarially prompted. A command shape that reaches a blocked
  action anyway is the highest-value report here.
- **Prompt injection that reaches a privileged action.** The agent reads email,
  web pages, chat messages and documents, all of which are attacker-controlled.
  Getting it to *say* something silly is a curiosity. Getting it to send, delete,
  push, spend, or exfiltrate is a vulnerability.
- **Credential exposure.** Anything that puts a token, key or secret somewhere it
  can be read - a log, an error message, a committed file, a prompt sent to a
  model provider.
- **Privilege escalation across the install boundary.** A plugin or an installed
  component reaching data or credentials outside its declared scope.
- **Anything that lets a third party reach an installed agent** without the
  operator's involvement.

## What is out of scope

- The use of `--dangerously-skip-permissions`. That is a deliberate, documented
  design decision with the hook layer as its compensating control - see
  [`docs/security.md`](docs/security.md). Reports that it is dangerous in the
  abstract, without a concrete bypass, will be closed.
- Findings that require an attacker to already have shell access on the machine
  running the agent. At that point they have everything.
- Missing hardening with no exploit path attached, particularly automated scanner
  output pasted verbatim.
- Vulnerabilities in third-party services or dependencies. Report those upstream;
  we will happily take a PR bumping a version.
- Social engineering of the maintainer.

## Disclosure

We would rather fix it and credit you than argue about a timeline. Tell us what
disclosure schedule you are working to and we will try to meet it. If you would
like credit, say so and how you want to be named.

## For operators running an install

If you believe **your own** install is compromised rather than finding a flaw in
the software, that is an incident, not a vulnerability report. Rotate your
credentials first, then get in touch. `docs/disaster-recovery.md` covers the
restore path.
