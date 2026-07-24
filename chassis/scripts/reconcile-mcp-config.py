#!/usr/bin/env python3
"""reconcile-mcp-config.py - detect + fix drift between a live .mcp.json and
what chassis.config.yaml + .mcp.json.template would hydrate.

Why this exists
===============
An install's live .mcp.json can drift from what the hydrator would emit today.
When it does, a full re-hydrate (bootstrap-mcp-config.sh --force, or any regen)
silently DROPS every server that is present in the live file but not enabled in
config. No error, no warning - working MCP integrations just vanish.

Concrete case (2026-07-24): a live .mcp.json running 15 servers, a config that
enables only 6. The 9-server gap are all in the template but gated behind module
flags the config never declared. A --force re-hydrate there would be destructive.

This tool makes that gap visible before it bites, and can write the missing
flags back into chassis.config.yaml so a later hydrate keeps every live server.

What it does
============
Reads the live .mcp.json, the .mcp.json.template, chassis.config.yaml, and .env.
It REUSES hydrate-mcp-json.py's gating logic (the `_enable_when` evaluator, the
placeholder substitution, the obsidian mode normalization) rather than
reimplementing it - there is one predicate evaluator in the chassis, and it
lives in the hydrator. See load_hydrator() for how it is imported.

It computes three sets by comparing the live server keys against the set the
hydrator WOULD emit for the given config + template:

  PRESENT_BUT_WOULD_DROP  in live, hydrate would NOT emit. The dangerous set.
                          For each, the exact `_enable_when` flag from the
                          template that would preserve it is resolved and
                          printed - the fix is to add that flag to config.
  WOULD_EMIT_BUT_MISSING  config/template says on, live lacks it. A re-hydrate
                          would ADD it. Under-provisioned; surfaced as info.
  CONSISTENT              present in both. Fine.

It also:
  - verifies, for every server that WOULD emit, that .env holds the
    <PLACEHOLDER> tokens it needs, so a later hydrate does not silently produce
    placeholder-broken entries (mirrors the hydrator's exit-2 condition).
  - flags host-vs-container path-model mismatches where detectable (a live entry
    carrying a host absolute path where the template renders a container path
    like ${CHASSIS_ROOT:-/app/chassis}/...).

Modes
=====
  --check (default)  report only. Human summary, or --json. Exit nonzero on any
                     drift. CI / smoke-test friendly. Read-only on everything.
  --fix              write the suggested flags into chassis.config.yaml
                     (idempotent, backup first, comments preserved). NEVER
                     writes .mcp.json - that stays the hydrator's job. --fix
                     only ever edits chassis.config.yaml.

Guardrails
==========
  - Read-only on .mcp.json. Always.
  - --fix only ever edits chassis.config.yaml, with a .bak backup written first.
  - Idempotent. Re-running against a reconciled install is a no-op.
  - No secrets in output - server names, config flag paths, and placeholder
    token NAMES only. Never a token value.

Exit codes
==========
  0 - consistent (check), or fix applied / nothing to fix
  1 - drift detected (check mode)
  2 - bad input (a required file is missing or malformed)
"""

import argparse
import importlib.util
import json
import os
import shutil
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HYDRATOR_PATH = os.path.join(SCRIPT_DIR, 'hydrate-mcp-json.py')


def load_hydrator():
    """Import hydrate-mcp-json.py as a module and return it.

    The hydrator's filename has a hyphen, so a plain `import` cannot name it.
    It IS cleanly importable by path, though: every function is module-level and
    main() is guarded by `if __name__ == '__main__'`, so importing runs no side
    effects. This is the exact mechanism chassis/second_brain/tests already use
    to drive the real hydrator. Importing rather than refactoring keeps
    hydrate-mcp-json.py byte-identical, so its existing test suite is untouched.
    """
    spec = importlib.util.spec_from_file_location('hydrate_mcp_json', HYDRATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_render_config(hydrator, config):
    """Return the config the hydrator would actually render against.

    hydrate-mcp-json.py's main() does one config normalization before it calls
    hydrate(): an obsidian backend with a non-adapter mode is forced to adapter,
    because obsidian has no native MCP server and direct mode would render no
    second-brain surface at all. To predict the emitted set faithfully, the
    reconciler applies the same normalization. It is replicated here (rather
    than imported) because it lives inside main() in the hydrator; kept small
    and adjacent to a pointer at the original so the coupling is visible.
    """
    if not isinstance(config, dict):
        return config
    render = json.loads(json.dumps(config))  # never mutate the caller's config
    sb = render.get('second_brain')
    if isinstance(sb, dict) and sb.get('backend') == 'obsidian':
        if sb.get('mode') != 'adapter':
            sb['mode'] = 'adapter'
    return render


# --------------------------------------------------------------------------
# Predicate parsing - which flag preserves a would-drop server
# --------------------------------------------------------------------------

def parse_clauses(hydrator, predicate):
    """Split an `_enable_when` predicate into structured clauses.

    Returns a list of dicts: {path, op, kind, value, raw_literal}. The RHS is
    parsed with the hydrator's OWN parse_literal so a literal means the same
    thing here as it does at hydrate time. Clauses whose RHS the hydrator cannot
    parse are dropped from the structured view (the hydrator treats the whole
    predicate as False in that case; we only use this view to SUGGEST a fix, and
    an unparsable literal has no safe suggestion).
    """
    out = []
    for raw in predicate.split('&&'):
        clause = raw.strip()
        if '!=' in clause and '==' not in clause:
            lhs, _, rhs = clause.partition('!=')
            op = '!='
        elif '==' in clause:
            lhs, _, rhs = clause.partition('==')
            op = '=='
        else:
            continue
        path = lhs.strip()
        if path.startswith('chassis.config.yaml.'):
            path = path[len('chassis.config.yaml.'):]
        parsed = hydrator.parse_literal(rhs)
        if parsed is None:
            continue
        kind, value = parsed
        out.append({
            'path': path,
            'op': op,
            'kind': kind,
            'value': value,
            'raw_literal': rhs.strip(),
        })
    return out


def yaml_value_literal(kind, value):
    """Render a parsed literal back into the text form written into YAML."""
    if kind == 'bool':
        return 'true' if value else 'false'
    if kind == 'str':
        return "'{}'".format(value)
    return str(value)


def resolve_fix(hydrator, entry, config):
    """Work out what config edits would make `entry` emit.

    Returns (assignments, notes, auto_fixable).
      assignments : list of {path, value_literal, raw_literal, op} for `==`
                    clauses that are NOT currently satisfied - the flags to add.
      notes       : human strings for clauses that cannot be auto-fixed safely.
      auto_fixable: True only when every unsatisfied clause is an `==` clause.

    `!=` clauses are deliberately NOT auto-applied. The only servers that gate on
    `!=` are the second-brain pair (siyuan/notion require
    second_brain.mode != 'adapter'); "fixing" one by flipping mode would silently
    drop the adapter server, trading one dropped server for another. Those get a
    note and are left to a human.
    """
    predicate = entry.get('_enable_when')
    if not predicate:
        return [], [], False
    assignments = []
    notes = []
    auto_fixable = True
    for clause in parse_clauses(hydrator, predicate):
        current = hydrator.resolve_path(config, clause['path'])
        if clause['op'] == '==':
            if current == clause['value']:
                continue  # already satisfied
            assignments.append({
                'path': clause['path'],
                'value_literal': yaml_value_literal(clause['kind'], clause['value']),
                'raw_literal': clause['raw_literal'],
                'op': '==',
            })
        else:  # !=
            if current != clause['value']:
                continue  # already satisfied
            auto_fixable = False
            notes.append(
                "requires {} != {} (currently {}) - resolve by hand; flipping it "
                "would drop the server it excludes".format(
                    clause['path'], clause['raw_literal'], repr(current))
            )
    return assignments, notes, auto_fixable


# --------------------------------------------------------------------------
# Placeholder check - would an emitted server hydrate broken
# --------------------------------------------------------------------------

def unresolved_placeholders_for(hydrator, entry, config, env):
    """Return the sorted set of <PLACEHOLDER> token names that would NOT resolve
    for this entry, mirroring what hydrate() would leave unresolved.

    Runs the hydrator's own apply_overrides -> strip_meta_keys ->
    substitute_placeholders pipeline against the entry, so the answer matches
    what the real hydrate would produce. Only token NAMES are returned, never
    values, so this is safe to print.
    """
    overridden = hydrator.apply_overrides(entry, config)
    stripped = hydrator.strip_meta_keys(overridden)
    unresolved = set()
    hydrator.substitute_placeholders(stripped, env, unresolved)
    return sorted(unresolved)


# --------------------------------------------------------------------------
# Host-vs-container path-model detection
# --------------------------------------------------------------------------

def _flatten_strings(obj, prefix=''):
    """Flatten a JSON-ish structure to {json_path: string_value} for leaves."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = '{}.{}'.format(prefix, k) if prefix else k
            out.update(_flatten_strings(v, child))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten_strings(v, '{}[{}]'.format(prefix, i)))
    elif isinstance(obj, str):
        out[prefix] = obj
    return out


_CONTAINER_MARKERS = ('${CHASSIS_ROOT', '/app/chassis', '/app/')


def _is_container_path(value):
    return any(marker in value for marker in _CONTAINER_MARKERS)


def _is_host_abs_path(value):
    """A leading-slash absolute path that is NOT the container root and carries
    no ${CHASSIS_ROOT} marker - i.e. a path baked for one host's namespace."""
    if not value.startswith('/'):
        return False
    if value.startswith('/app/'):
        return False
    if '${CHASSIS_ROOT' in value:
        return False
    return True


def detect_path_model_mismatches(name, rendered_entry, live_entry):
    """Compare a server present in both the render and live for host-vs-container
    path drift. Returns a list of mismatch dicts (possibly empty).

    The signal: the template renders a value carrying a container-root marker
    (the hydrator does NOT expand ${CHASSIS_ROOT}, so it survives verbatim into
    the emitted file), while the live file at the same field holds a host
    absolute path. That is a file written for the wrong namespace.
    """
    rendered = _flatten_strings(rendered_entry)
    live = _flatten_strings(live_entry)
    mismatches = []
    for field, tval in rendered.items():
        lval = live.get(field)
        if lval is None:
            continue
        if _is_container_path(tval) and _is_host_abs_path(lval):
            mismatches.append({
                'server': name,
                'field': field,
                'template': tval,
                'live': lval,
            })
    return mismatches


# --------------------------------------------------------------------------
# Core drift computation
# --------------------------------------------------------------------------

def compute_report(hydrator, live, config, template, env):
    """Build the full drift report. Pure - no I/O, no exit."""
    render_config = normalize_render_config(hydrator, config)
    dropped_env = {}
    hydrated, _unresolved = hydrator.hydrate(render_config, template, env, dropped_env)
    would_emit = set(hydrated['mcpServers'])

    live_servers = live.get('mcpServers', {}) if isinstance(live, dict) else {}
    live_keys = {k for k in live_servers if not k.startswith('_')}

    template_servers = template.get('mcpServers', {})

    present_but_would_drop = []
    for name in sorted(live_keys - would_emit):
        entry = template_servers.get(name)
        if not isinstance(entry, dict):
            present_but_would_drop.append({
                'server': name,
                'enable_when': None,
                'assignments': [],
                'notes': ['not present in .mcp.json.template - cannot resolve a '
                          'preserving flag; carried by hand or from an old template'],
                'auto_fixable': False,
            })
            continue
        assignments, notes, auto_fixable = resolve_fix(hydrator, entry, render_config)
        present_but_would_drop.append({
            'server': name,
            'enable_when': entry.get('_enable_when'),
            'assignments': assignments,
            'notes': notes,
            'auto_fixable': auto_fixable and bool(assignments),
        })

    would_emit_but_missing = sorted(would_emit - live_keys)
    consistent = sorted(live_keys & would_emit)

    broken_placeholders = {}
    for name in sorted(would_emit):
        entry = template_servers.get(name)
        if not isinstance(entry, dict):
            continue
        missing = unresolved_placeholders_for(hydrator, entry, render_config, env)
        if missing:
            broken_placeholders[name] = missing

    path_mismatches = []
    for name in consistent:
        entry = template_servers.get(name)
        if not isinstance(entry, dict):
            continue
        rendered_entry = hydrated['mcpServers'].get(name, {})
        path_mismatches.extend(
            detect_path_model_mismatches(name, rendered_entry, live_servers.get(name, {}))
        )

    drift = bool(present_but_would_drop or would_emit_but_missing
                 or broken_placeholders or path_mismatches)

    return {
        'present_but_would_drop': present_but_would_drop,
        'would_emit_but_missing': would_emit_but_missing,
        'consistent': consistent,
        'broken_placeholders': broken_placeholders,
        'path_model_mismatches': path_mismatches,
        'drift': drift,
    }


# --------------------------------------------------------------------------
# chassis.config.yaml surgical editing (--fix)
# --------------------------------------------------------------------------

_ADDED_MARKER = '  # added by reconcile-mcp-config'


def _indent_len(line):
    return len(line) - len(line.lstrip(' '))


def _key_of(line):
    """Return the mapping key on this line, or None for blanks, comments, and
    list items. Keys in chassis.config.yaml are simple identifiers."""
    content = line.strip()
    if not content or content.startswith('#') or content.startswith('- '):
        return None
    if ':' not in content:
        return None
    key = content.split(':', 1)[0].strip()
    if not key or key.startswith('#'):
        return None
    return key


def _block_end(lines, parent_idx, parent_indent):
    """Index one past the last line belonging to the block whose header is at
    parent_idx. Stops at the next real key indented at or above parent_indent."""
    j = parent_idx + 1
    while j < len(lines):
        content = lines[j].strip()
        if content == '' or content.startswith('#'):
            j += 1
            continue
        if _indent_len(lines[j]) <= parent_indent:
            break
        j += 1
    return j


def _find_child(lines, key, region_start, region_end, indent):
    for i in range(region_start, region_end):
        if _indent_len(lines[i]) == indent and _key_of(lines[i]) == key:
            return i
    return -1


def _replace_value(line, value_literal):
    """Replace the value on a `key: value` line, preserving indentation and any
    trailing inline comment at its original column."""
    indent = _indent_len(line)
    indent_str = line[:indent]
    body = line[indent:]
    key = body.split(':', 1)[0]
    after = body[len(key) + 1:]
    hashpos = after.find('#')
    new_body = '{}{}: {}'.format(indent_str, key, value_literal)
    if hashpos >= 0:
        comment = after[hashpos:]
        comment_col = indent + len(key) + 1 + hashpos
        pad = comment_col - len(new_body)
        if pad < 1:
            pad = 1
        return '{}{}{}'.format(new_body, ' ' * pad, comment)
    return new_body


def set_yaml_path(lines, segments, value_literal):
    """Set (or insert) segments -> value_literal in `lines`, mutating in place.

    Returns 'modified', 'inserted', or 'noop'. Existing leaves are rewritten in
    place; missing leaves and missing intermediate mappings are inserted as the
    first child of their parent, tagged with a trailing marker comment.
    """
    parent_idx = -1
    parent_indent = -2
    region_start, region_end = 0, len(lines)
    for depth, seg in enumerate(segments):
        child_indent = parent_indent + 2
        is_leaf = depth == len(segments) - 1
        idx = _find_child(lines, seg, region_start, region_end, child_indent)
        if is_leaf:
            if idx >= 0:
                new_line = _replace_value(lines[idx], value_literal)
                if new_line == lines[idx]:
                    return 'noop'
                lines[idx] = new_line
                return 'modified'
            insert_at = parent_idx + 1 if parent_idx >= 0 else 0
            indent_str = ' ' * child_indent
            lines.insert(insert_at,
                         '{}{}: {}{}'.format(indent_str, seg, value_literal, _ADDED_MARKER))
            return 'inserted'
        if idx >= 0:
            parent_idx = idx
            parent_indent = child_indent
            region_start = idx + 1
            region_end = _block_end(lines, idx, child_indent)
        else:
            insert_at = parent_idx + 1 if parent_idx >= 0 else 0
            indent_str = ' ' * child_indent
            lines.insert(insert_at, '{}{}:'.format(indent_str, seg))
            parent_idx = insert_at
            parent_indent = child_indent
            region_start = insert_at + 1
            region_end = insert_at + 1
    return 'noop'


def gather_fix_assignments(report):
    """Collect the unique (path, value_literal) assignments to apply, in a
    stable order, from the auto-fixable would-drop servers only."""
    seen = {}
    ordered = []
    for item in report['present_but_would_drop']:
        if not item['auto_fixable']:
            continue
        for assignment in item['assignments']:
            key = assignment['path']
            if key in seen:
                continue
            seen[key] = assignment['value_literal']
            ordered.append((assignment['path'], assignment['value_literal']))
    return ordered


def apply_fix(config_path, report):
    """Write the suggested flags into chassis.config.yaml. Backs the file up to
    <path>.bak first, only when there is something to change. Returns a dict
    describing what happened."""
    assignments = gather_fix_assignments(report)
    with open(config_path) as f:
        text = f.read()
    lines = text.split('\n')

    applied = []
    for path, value_literal in assignments:
        action = set_yaml_path(lines, path.split('.'), value_literal)
        if action != 'noop':
            applied.append({'path': path, 'value': value_literal, 'action': action})

    skipped = [item['server'] for item in report['present_but_would_drop']
               if not item['auto_fixable']]

    if not applied:
        return {'changed': False, 'applied': [], 'skipped': skipped, 'backup': None}

    backup_path = config_path + '.bak'
    shutil.copy2(config_path, backup_path)
    with open(config_path, 'w') as f:
        f.write('\n'.join(lines))
    return {'changed': True, 'applied': applied, 'skipped': skipped, 'backup': backup_path}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_human(report):
    lines = []
    pd = report['present_but_would_drop']
    lines.append('=== MCP config drift report ===')
    lines.append('')
    if pd:
        lines.append('PRESENT_BUT_WOULD_DROP ({}) - live now, a re-hydrate would DROP these:'.format(len(pd)))
        for item in pd:
            lines.append('  - {}'.format(item['server']))
            if item['enable_when']:
                lines.append('      gate: {}'.format(item['enable_when']))
            for assignment in item['assignments']:
                lines.append('      add to config: {}: {}'.format(
                    assignment['path'], assignment['value_literal']))
            for note in item['notes']:
                lines.append('      note: {}'.format(note))
        lines.append('')
    else:
        lines.append('PRESENT_BUT_WOULD_DROP (0) - nothing live would be dropped.')
        lines.append('')

    wm = report['would_emit_but_missing']
    if wm:
        lines.append('WOULD_EMIT_BUT_MISSING ({}) - config says on, live lacks them (info; a re-hydrate adds them):'.format(len(wm)))
        for name in wm:
            lines.append('  - {}'.format(name))
        lines.append('')

    lines.append('CONSISTENT ({}): {}'.format(
        len(report['consistent']), ', '.join(report['consistent']) or '(none)'))
    lines.append('')

    bp = report['broken_placeholders']
    if bp:
        lines.append('PLACEHOLDER-BROKEN ({}) - would emit but .env is missing tokens:'.format(len(bp)))
        for name, tokens in bp.items():
            lines.append('  - {}: {}'.format(name, ', '.join(tokens)))
        lines.append('')

    pm = report['path_model_mismatches']
    if pm:
        lines.append('PATH_MODEL_MISMATCH ({}) - live host path where template renders a container path:'.format(len(pm)))
        for item in pm:
            lines.append('  - {} [{}]'.format(item['server'], item['field']))
            lines.append('      template: {}'.format(item['template']))
            lines.append('      live:     {}'.format(item['live']))
        lines.append('')

    if report['drift']:
        lines.append('DRIFT DETECTED. Run with --fix to write the preserving flags into chassis.config.yaml.')
    else:
        lines.append('No drift. Live .mcp.json matches what the hydrator would emit.')
    return '\n'.join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def resolve_default(*candidates):
    for c in candidates:
        if c:
            return c
    return None


def main(argv=None):
    customer_home = os.environ.get('CUSTOMER_HOME') or os.environ.get('CHASSIS_HOME') or '.'
    default_template = os.path.join(os.path.dirname(SCRIPT_DIR), '.mcp.json.template')

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', default=os.path.join(customer_home, 'chassis.config.yaml'),
                   help='chassis.config.yaml path')
    p.add_argument('--template', default=default_template, help='.mcp.json.template path')
    p.add_argument('--env', default=os.path.join(customer_home, '.env'),
                   help='.env path (for the placeholder check)')
    p.add_argument('--mcp', default=os.path.join(customer_home, '.mcp.json'),
                   help='live .mcp.json path (read-only)')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='report only (default)')
    mode.add_argument('--fix', action='store_true',
                      help='write preserving flags into chassis.config.yaml')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    args = p.parse_args(argv)

    hydrator = load_hydrator()

    for label, path in (('template', args.template), ('live .mcp.json', args.mcp)):
        if not os.path.exists(path):
            print('ERROR: {} not found: {}'.format(label, path), file=sys.stderr)
            return 2

    try:
        template = hydrator.load_json(args.template)
    except (ValueError, OSError) as exc:
        print('ERROR: cannot parse template {}: {}'.format(args.template, exc), file=sys.stderr)
        return 2
    try:
        live = hydrator.load_json(args.mcp)
    except (ValueError, OSError) as exc:
        print('ERROR: cannot parse live .mcp.json {}: {}'.format(args.mcp, exc), file=sys.stderr)
        return 2

    if os.path.exists(args.config):
        config = hydrator.load_yaml(args.config)
    else:
        print('WARN: config not found: {} - treating as empty. Feature-gated '
              'servers count as would-drop.'.format(args.config), file=sys.stderr)
        config = {}

    env = hydrator.load_env(args.env)

    report = compute_report(hydrator, live, config, template, env)

    if args.fix:
        if not os.path.exists(args.config):
            print('ERROR: --fix needs an existing chassis.config.yaml to edit: {}'.format(
                args.config), file=sys.stderr)
            return 2
        result = apply_fix(args.config, report)
        if args.json:
            print(json.dumps({'report': report, 'fix': result}, indent=2))
        elif result['changed']:
            print('Wrote {} flag(s) into {} (backup: {}):'.format(
                len(result['applied']), args.config, result['backup']))
            for item in result['applied']:
                print('  {} {}: {}'.format(item['action'], item['path'], item['value']))
            if result['skipped']:
                print('Not auto-fixed (resolve by hand): {}'.format(', '.join(result['skipped'])))
            print('Re-run bootstrap-mcp-config.sh --dry-run to confirm every live server now emits.')
        else:
            print('No changes needed - chassis.config.yaml already preserves every live server.')
            if result['skipped']:
                print('Still needs a manual decision: {}'.format(', '.join(result['skipped'])))
        return 0

    # --check (default)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_human(report))
    return 1 if report['drift'] else 0


if __name__ == '__main__':
    sys.exit(main())
