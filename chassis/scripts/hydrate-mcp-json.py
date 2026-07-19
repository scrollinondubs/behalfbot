#!/usr/bin/env python3
"""hydrate-mcp-json.py - Build customer .mcp.json from template + chassis config + .env.

Replaces the bootstrap.sh `hydrate_mcp_json()` TODO stub.

Algorithm:
1. Load chassis.config.yaml (resolved customer-side path passed in).
2. Load .mcp.json.template (chassis-side path passed in).
3. Load .env (customer-side, optional) for placeholder substitution.
4. For each mcpServers entry:
   - Skip entries whose key starts with '_' (template-only dividers).
   - If entry has `_enable_when`, evaluate the predicate against the config;
     drop the entry if false.
   - If entry has `_override_when`, apply each clause whose predicate holds,
     rewriting individual keys inside the kept entry (see apply_overrides).
   - Strip all keys starting with '_' from the kept entry.
   - Substitute <PLACEHOLDER> tokens in string values from .env (or env vars).
     Tokens whose substitution value is missing are LEFT IN PLACE - bootstrap
     should warn loud but not silently emit a broken config.
5. Write hydrated JSON to the output path.

Usage:
    python3 hydrate-mcp-json.py \\
        --config /path/to/chassis.config.yaml \\
        --template /path/to/.mcp.json.template \\
        --env /path/to/.env \\
        --output /path/to/.mcp.json [--dry-run]

Exit codes:
    0 - hydrated successfully
    1 - bad input (file missing, malformed YAML/JSON)
    2 - unresolved placeholders detected (file still written; bootstrap should warn)
"""

import argparse
import json
import os
import re
import sys


def load_yaml(path):
    """Parse chassis.config.yaml. PyYAML when available, fallback parser otherwise.

    This used to `sys.exit(1)` when PyYAML was missing, which is what forced
    bootstrap-mcp-config.sh to keep a second, worse renderer around for the
    no-PyYAML case: a sed+jq pass that stripped `_enable_when` without
    evaluating it and therefore registered every gated server at once - siyuan
    AND notion AND secondbrain on the same install. Two renderers with two
    different answers is a worse failure than a degraded parser, so the
    dependency is now soft and there is exactly one renderer.

    The fallback is `chassis.second_brain.factory._parse_yaml_minimal`, already
    shipped and already tested for exactly this subtree. It does not support
    lists; `_enable_when` predicates only ever address scalar leaves
    (`modules.google.gmail == true`, `second_brain.backend == 'siyuan'`), so a
    list-valued key resolves to None and its clause evaluates False - the same
    conservative direction the predicate evaluator already takes for a missing
    key.
    """
    try:
        import yaml
    except ImportError:
        pass
    else:
        with open(path) as f:
            return yaml.safe_load(f) or {}

    package_parent = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    if package_parent not in sys.path:
        sys.path.insert(0, package_parent)
    try:
        from chassis.second_brain.factory import _parse_yaml_minimal
    except ImportError:
        print('ERROR: PyYAML not installed and chassis.second_brain is not '
              'importable, so chassis.config.yaml cannot be parsed. '
              'apt-get install python3-yaml OR pip install pyyaml',
              file=sys.stderr)
        sys.exit(1)
    print('WARN: PyYAML not installed - parsing chassis.config.yaml with the '
          'minimal fallback parser (scalars and nested mappings only).',
          file=sys.stderr)
    with open(path) as f:
        return _parse_yaml_minimal(f.read()) or {}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_env(path):
    """Parse a .env file into a dict. Tolerates `export FOO=bar`, comments,
    quoted values. Empty values become ''."""
    env = {}
    if not path or not os.path.exists(path):
        return env
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[len('export '):]
            if '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            env[key] = value
    return env


def resolve_path(config, dotted):
    """Resolve a dotted path like 'chassis.config.yaml.modules.research.brave'
    against the loaded config. Strips the `chassis.config.yaml.` prefix.

    Returns None if any segment is missing. Returns the leaf value otherwise.
    """
    if dotted.startswith('chassis.config.yaml.'):
        dotted = dotted[len('chassis.config.yaml.'):]
    cur = config
    for segment in dotted.split('.'):
        if isinstance(cur, dict) and segment in cur:
            cur = cur[segment]
        else:
            return None
    return cur


_LITERAL_PAT = re.compile(r"""
    ^                                # start
    (?:
        (?P<bool>true|false)         # boolean literal
        | '(?P<sq>[^']*)'            # single-quoted string
        | "(?P<dq>[^"]*)"            # double-quoted string
        | (?P<bare>[\w\-]+)          # bareword (number-ish, identifier)
    )
    $                                # end
""", re.VERBOSE)


def parse_literal(token):
    """Parse a literal token from an `_enable_when` RHS.

    Returns (kind, value):
        kind ∈ {'bool', 'str', 'bare'}
        value is the parsed Python value.
    """
    m = _LITERAL_PAT.match(token.strip())
    if not m:
        return None
    if m.group('bool'):
        return ('bool', m.group('bool') == 'true')
    if m.group('sq') is not None:
        return ('str', m.group('sq'))
    if m.group('dq') is not None:
        return ('str', m.group('dq'))
    if m.group('bare'):
        return ('bare', m.group('bare'))
    return None


def evaluate_clause(clause, config):
    """Evaluate one `<dotted.path> == <literal>` or `<dotted.path> != <literal>` clause.

    Missing-key semantics differ by operator, deliberately:
      - `==` on a missing path is False (an absent flag never enables).
      - `!=` on a missing path is True (None != literal) - this is what lets
        `second_brain.mode != 'adapter'` keep today's servers registered on
        configs that predate the mode key.
    """
    if '!=' in clause and '==' not in clause:
        lhs, _, rhs = clause.partition('!=')
        parsed = parse_literal(rhs)
        if parsed is None:
            return False
        _, expected = parsed
        return resolve_path(config, lhs.strip()) != expected
    if '==' in clause:
        lhs, _, rhs = clause.partition('==')
        parsed = parse_literal(rhs)
        if parsed is None:
            return False
        _, expected = parsed
        return resolve_path(config, lhs.strip()) == expected
    return False


def evaluate_enable_when(predicate, config):
    """Evaluate an `_enable_when` predicate against the loaded config.

    Supported grammar: one or more `<dotted.path> == <literal>` /
    `<dotted.path> != <literal>` clauses joined by `&&` (all must hold).
    Conservatively returns False on any unrecognized predicate shape - we
    prefer dropping an entry over emitting a broken one when the predicate
    cannot be parsed.
    """
    clauses = [c.strip() for c in predicate.split('&&')]
    if not clauses or any(not c for c in clauses):
        return False
    return all(evaluate_clause(clause, config) for clause in clauses)


def apply_overrides(entry, config):
    """Apply an entry's `_override_when` clauses against the config.

    `_enable_when` decides whether a server is registered at all. It cannot
    vary a value *inside* a registered server, which is what a capability tier
    needs: one `google-calendar` server whose exposed toolset widens when the
    installer raises `trust_line.calendar` to `read_write`. Splitting that into
    two differently-named servers would change the MCP tool prefix and break
    every prompt written against the read-only one, so the value moves instead
    of the server.

    Shape:

        "_override_when": [
          {
            "predicate": "chassis.config.yaml.trust_line.calendar == 'read_write'",
            "set": {"env.ENABLED_TOOLS": "list-events,create-event,..."}
          }
        ]

    `set` keys are dotted paths *within the entry*. Clauses apply in order, so
    a later clause wins over an earlier one. A clause whose predicate is false,
    unparsable, or whose config path is missing is skipped - the entry keeps
    the value it declares inline, which is why the template's inline value must
    always be the safe floor (read-only), never the privileged tier.
    """
    clauses = entry.get('_override_when')
    if not isinstance(clauses, list):
        return entry
    out = json.loads(json.dumps(entry))  # deep copy - never mutate the template
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        predicate = clause.get('predicate')
        assignments = clause.get('set')
        if not isinstance(predicate, str) or not isinstance(assignments, dict):
            continue
        if not evaluate_enable_when(predicate, config):
            continue
        for dotted, value in assignments.items():
            segments = dotted.split('.')
            cur = out
            for segment in segments[:-1]:
                nxt = cur.get(segment)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[segment] = nxt
                cur = nxt
            cur[segments[-1]] = value
    return out


_PLACEHOLDER_PAT = re.compile(r'<([A-Z_][A-Z0-9_]*)>')


def substitute_placeholders(value, env, unresolved):
    """Replace <PLACEHOLDER> tokens with env values. Records unresolved tokens."""
    if isinstance(value, str):
        def repl(match):
            key = match.group(1)
            if key in env:
                return env[key]
            if key in os.environ:
                return os.environ[key]
            unresolved.add(key)
            return match.group(0)  # leave as-is
        return _PLACEHOLDER_PAT.sub(repl, value)
    if isinstance(value, list):
        return [substitute_placeholders(v, env, unresolved) for v in value]
    if isinstance(value, dict):
        return {k: substitute_placeholders(v, env, unresolved)
                for k, v in value.items()}
    return value


# Env var names that carry a secret. An unresolved placeholder in one of these
# becomes an Authorization header, so it must never survive into the output.
# Names outside this set (GOOGLE_OAUTH_CREDENTIALS, SIYUAN_URL, *_DIR, ...) are
# paths and hosts: a leftover placeholder there is visibly wrong at startup and
# leaks nothing, so it keeps the existing pass-through-and-warn behavior.
_CREDENTIAL_NAME_PAT = re.compile(r'(TOKEN|SECRET|PASSWORD|_KEY|APIKEY|_PAT)$')


def drop_unresolved_env(entry, unresolved):
    """Remove credential env keys whose value still carries a <PLACEHOLDER>.

    An unresolved placeholder used to survive into the output verbatim, so a
    server got `"NOTION_API_TOKEN": "<NOTION_API_TOKEN>"` in its environment and
    sent that literal string as its bearer token. Every call 401'd while the
    config looked correctly filled in.

    Dropping the key gives the server the same view it would have had if nobody
    had written a placeholder at all - the var is simply unset - and unset is a
    failure mode consumers already handle loudly (the second_brain factory
    raises on an empty token).
    """
    env = entry.get('env')
    if not isinstance(env, dict) or not unresolved:
        return entry, []
    tokens = {f'<{key}>' for key in unresolved}
    dropped = [k for k, v in env.items()
               if isinstance(v, str)
               and _CREDENTIAL_NAME_PAT.search(k)
               and any(t in v for t in tokens)]
    if not dropped:
        return entry, []
    entry = dict(entry)
    entry['env'] = {k: v for k, v in env.items() if k not in dropped}
    return entry, dropped


def strip_meta_keys(entry):
    """Drop keys starting with `_` from a dict (template-only metadata)."""
    return {k: v for k, v in entry.items() if not k.startswith('_')}


def hydrate(config, template, env, dropped_env=None):
    """Build the hydrated mcpServers dict and return (output, unresolved).

    Pass a dict as `dropped_env` to collect server name -> list of env keys
    removed because their value did not resolve. It is an out-parameter rather
    than a third return value so the existing two-tuple contract holds for
    callers that do not care. See drop_unresolved_env.
    """
    unresolved = set()
    out = {}
    servers = template.get('mcpServers', {})
    for name, entry in servers.items():
        if name.startswith('_'):
            continue  # divider/placeholder entry
        if not isinstance(entry, dict):
            continue
        predicate = entry.get('_enable_when')
        if predicate is not None:
            if not evaluate_enable_when(predicate, config):
                continue
        overridden = apply_overrides(entry, config)
        stripped = strip_meta_keys(overridden)
        substituted = substitute_placeholders(stripped, env, unresolved)
        substituted, dropped = drop_unresolved_env(substituted, unresolved)
        if dropped and dropped_env is not None:
            dropped_env[name] = dropped
        out[name] = substituted
    return {'mcpServers': out}, unresolved


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', required=True, help='chassis.config.yaml path')
    p.add_argument('--template', required=True, help='.mcp.json.template path')
    p.add_argument('--env', default=None, help='.env path (optional)')
    p.add_argument('--output', required=True, help='target .mcp.json path')
    p.add_argument('--dry-run', action='store_true',
                   help='print hydrated JSON to stdout, do not write')
    args = p.parse_args()

    if not os.path.exists(args.template):
        print(f'ERROR: template not found: {args.template}', file=sys.stderr)
        sys.exit(1)

    # A missing chassis.config.yaml is the disaster-recovery case
    # bootstrap-mcp-config.sh's fallback renderer was written for: reconstruct
    # a usable .mcp.json on an install whose config is gone. Treat it as an
    # empty config rather than an error, so that path can delegate here too.
    #
    # Empty config is the SAFE direction, and only because of how the predicate
    # evaluator already treats a missing key: `== ` clauses go False (a gated
    # server is dropped, so no siyuan, no notion, no secondbrain, no Google)
    # while `!=` clauses go True. The result is core servers only, which is
    # exactly what a recovery should produce - a minimal working config the
    # operator widens on purpose, not every server at once.
    if not os.path.exists(args.config):
        print(f'WARN: config not found: {args.config} - rendering with an empty '
              f'config. Feature-gated servers will be OMITTED. Restore '
              f'chassis.config.yaml and re-run to get the full set.',
              file=sys.stderr)
        config = {}
    else:
        config = load_yaml(args.config)
    template = load_json(args.template)
    env = load_env(args.env)

    dropped_env = {}
    hydrated, unresolved = hydrate(config, template, env, dropped_env)
    output_text = json.dumps(hydrated, indent=2)

    if args.dry_run:
        print(output_text)
    else:
        with open(args.output, 'w') as f:
            f.write(output_text)
            f.write('\n')
        print(f'wrote {args.output} '
              f'({len(hydrated["mcpServers"])} mcpServers)')

    if dropped_env:
        for server, keys in sorted(dropped_env.items()):
            print(f'WARN: {server}: dropped env {sorted(keys)} - the value did '
                  f'not resolve.', file=sys.stderr)
        print('      These vars are now UNSET for that server rather than being '
              'set to the\n'
              '      literal placeholder text. A credential placeholder shipped '
              'as a value is\n'
              '      a bearer token that 401s on every call while the config '
              'looks correct.',
              file=sys.stderr)

    if unresolved:
        print(f'WARN: unresolved placeholders left in output: '
              f'{sorted(unresolved)}', file=sys.stderr)
        print(f'      these are <TOKEN> values not found in --env or os.environ. '
              f'Fix .env and re-run.', file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == '__main__':
    main()
