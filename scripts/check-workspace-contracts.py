"""Read source AST contracts/inserts for review; never import application code or access data.

Usage: python scripts/check-workspace-contracts.py <repo_root> <out.json> [frozen-w2-contracts.json]
The manifest argument defaults to frozen-w2-contracts.json next to this script.
"""
import ast
import collections
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2])
frozen_contracts = {}
manifest_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(__file__).resolve().parent / 'frozen-w2-contracts.json'
if manifest_path.is_file():
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    for name, functions in manifest['scope_and_flag'].items():
        for function in functions.split():
            frozen_contracts[(root / name, function)] = ({'scope', 'multi_workspace_enabled'}, True)
    for name in manifest['exports']:
        for function in ('export_page', 'validate_export_fences'):
            frozen_contracts[(root / name, function)] = ({'scope', 'multi_workspace_enabled'}, False)
    for name in manifest['operator']:
        frozen_contracts[(root / name, 'observability_quality_summary')] = ({'instance_operator'}, False)
    for name, functions in manifest['scope_only'].items():
        for function in functions.split():
            frozen_contracts[(root / name, function)] = ({'scope'}, False)
watch = {'scope', 'multi_workspace_enabled', 'access_fence', 'instance_operator',
         'source_fence', 'expected_raw_uri', 'expected_mime_type', 'workspace_id'}
files = []
for folder in ('core', 'modules', 'apps'):
    for directory, children, names in os.walk(root / folder):
        children[:] = [c for c in children if c not in {'.venv', 'node_modules', '__pycache__', '.next'}]
        files.extend(Path(directory) / name for name in names if name.endswith('.py'))
trees = {p: ast.parse(p.read_text(encoding='utf-8-sig'), filename=str(p)) for p in files}
defs = {(p, n.name): n for p, t in trees.items() for n in t.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
models = {}
for p, tree in trees.items():
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                continue
            if (item.target.id == 'workspace_id' and isinstance(item.value, ast.Call)
                    and any(k.arg == 'nullable' and isinstance(k.value, ast.Constant)
                            and k.value.value is False for k in item.value.keywords)):
                models[(p, node.name)] = True

def modfile(mod):
    """Resolve only repository-owned absolute modules without executing imports."""
    base = root.joinpath(*mod.split('.'))
    for candidate in (base.with_suffix('.py'), base / '__init__.py'):
        if candidate in trees:
            return candidate
    return None

def enclosing(tree, node):
    """Find the narrowest named function spanning a call for explicit reserved-body review."""
    candidates = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.lineno <= node.lineno <= n.end_lineno]
    return min(candidates, key=lambda n: n.end_lineno - n.lineno).name if candidates else '<module>'

calls, inserts, unresolved, frozen_calls, frozen_unresolved = [], [], [], [], []
for path, tree in trees.items():
    aliases = {}
    named_functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            for alias in n.names:
                mf = modfile(n.module + '.' + alias.name)
                if mf:
                    aliases[alias.asname or alias.name] = ('mod', mf)
                elif modfile(n.module):
                    aliases[alias.asname or alias.name] = ('fn', modfile(n.module), alias.name)
        elif isinstance(n, ast.Import):
            for alias in n.names:
                mf = modfile(alias.name)
                if alias.asname and mf:
                    aliases[alias.asname] = ('mod', mf)
    local = {name for (p, name) in defs if p == path}

    def target_of(fn, aliases=aliases, local=local, path=path):  # bound now; only called in this iteration
        """Resolve direct and module-aliased calls; dynamic dispatch remains manual review."""
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            alias = aliases.get(fn.value.id)
            if alias and alias[0] == 'mod':
                return alias[1], fn.attr
        if isinstance(fn, ast.Name):
            if fn.id in local or (path, fn.id) in models:
                return path, fn.id
            alias = aliases.get(fn.id)
            if alias and alias[0] == 'fn':
                return alias[1], alias[2]
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = target_of(node.func)
        given = {k.arg for k in node.keywords}
        candidates = [n for n in named_functions if n.lineno <= node.lineno <= n.end_lineno]
        where = {'file': path.relative_to(root).as_posix(), 'line': node.lineno,
                 'function': min(candidates, key=lambda n: n.end_lineno - n.lineno).name if candidates else '<module>'}
        if target in frozen_contracts:
            required, drop_owner = frozen_contracts[target]
            if None in given or any(isinstance(a, ast.Starred) for a in node.args):
                frozen_unresolved.append({**where, 'callee': target[1], 'reason': 'expanded_args'})
            else:
                missing = sorted(required - given)
                retained_owner = drop_owner and 'owner_id' in given
                excess = 0
                if target in defs and drop_owner and defs[target].args.vararg is None:
                    positional = [a for a in defs[target].args.posonlyargs + defs[target].args.args
                                  if a.arg != 'owner_id']
                    excess = max(0, len(node.args) - len(positional))
                if missing or retained_owner or excess:
                    frozen_calls.append({**where, 'callee_file': target[0].relative_to(root).as_posix(),
                                         'callee': target[1], 'missing': missing,
                                         'retained_owner_keyword': bool(retained_owner),
                                         'excess_positional_after_owner_removal': excess})
        if target in defs:
            if None in given or any(isinstance(a, ast.Starred) for a in node.args):
                unresolved.append({**where, 'reason': 'expanded_args', 'callee': target[1]})
            else:
                args = defs[target].args
                required = {k.arg for k, default in zip(args.kwonlyargs, args.kw_defaults)
                            if default is None and k.arg in watch}
                accepted = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
                missing = sorted(required - given)
                unexpected = sorted(given - accepted) if args.kwarg is None else []
                positional = args.posonlyargs + args.args
                duplicate = sorted(given.intersection(a.arg for a in positional[:len(node.args)]))
                excess = max(0, len(node.args) - len(positional)) if args.vararg is None else 0
                if missing or unexpected or duplicate or excess:
                    calls.append({**where, 'callee_file': target[0].relative_to(root).as_posix(),
                                  'callee': target[1], 'missing': missing, 'unexpected': unexpected,
                                  'duplicate_arguments': duplicate, 'excess_positional': excess})
        model_target = target
        values = node
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'values':
            base = node.func.value
            if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == 'insert' and base.args:
                model_target = target_of(base.args[0])
        if model_target in models and 'workspace_id' not in given and None not in given:
            inserts.append({**where, 'model': model_target[1],
                            'model_file': model_target[0].relative_to(root).as_posix()})
data = {'root': str(root), 'static_calls': calls, 'heuristic_inserts': inserts,
        'expanded_calls_for_manual_review': unresolved,
        'frozen_contract_calls': frozen_calls, 'frozen_expanded_calls_for_manual_review': frozen_unresolved,
        'limits': 'AST direct calls only; dynamic dispatch, missing positional bindings, alias ambiguity and insert chains require source review.'}
output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'static_calls': len(calls), 'heuristic_inserts': len(inserts),
                  'call_files': dict(collections.Counter(x['file'] for x in calls)),
                  'frozen_contract_calls': len(frozen_calls),
                  'expanded_calls': len(unresolved), 'output': str(output)}))
