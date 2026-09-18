# wharf

wharf is the distribution hub of perish.code. It sits above Plumb: it distributes
Plumb, so Plumb does not govern it. This file is the only unstructured document in
the repository; `CLAUDE.md` only points here.

## Shape

| Path | Holds |
| --- | --- |
| `.github/workflows/` | orchestration, one workflow per distribution path |
| `lib/` | every piece of shared logic, one module per function |
| `scripts/` | thin entry points run as `python -m scripts.<name>` from the repository root |
| `tests/` | tests mirroring `lib/` and `scripts/` |
| `resources/` | non-code files, read only through `lib/resources.py` |
| `.githooks/pre-commit` | the only gate |

`scripts` may import `lib`; `lib` never imports `scripts`; scripts never import each
other; `lib` has no import cycles. Only `lib/resources.py` knows where files live.

## Working rules

- `main` is production. Merging is deploying; a bad change is reverted.
- Python in `lib` holds atomic actions. An action owns only its own arguments,
  outputs and preconditions, refuses early with `Refusal`, and knows nothing about
  other actions or their order. Workflows own ordering, conditions and composition.
- No comments or docstrings anywhere, including workflow YAML. Names carry meaning;
  facts that need structure live in `resources/`.
- Keep Ectropy-level limits: at most 300 lines per file, 4 parameters per function,
  4 nested blocks, 10 entries per directory and 3 levels below a top-level directory.
- Words claimed elsewhere in perish.code are listed in `resources/vocabulary.json`
  and must not name anything here.
- Third-party Python dependencies are allowed only when locked to exact versions and
  hashes. Actions are official `actions/*` only, pinned by SHA in
  `resources/actions.json` together with the `runs.using` of that SHA's
  `action.yml`; only `node24` and `composite` are accepted, so a Node 20 action
  never enters. Prefer tools already on the runner over adding an action.
- Enable the gate with `git config core.hooksPath .githooks`. It runs
  `python3 -B -m scripts.selfcheck` and the test suite.

## Workloads

A workload key is `sha256` over the canonical JSON of the hash version, the basis
and the implementation. The basis is the effective content an entry resolves to,
recorded item by item, widened rather than narrowed where resolution is imprecise.
The implementation is the content of the action module, its `lib` import closure
and any resources it reads. Records live under `workload/<hash_version>/<key>/` as
`basis.json`, `blobs/*` and finally `record.json`; every write is create-only, and
different bytes under an existing key refuse as non-determinism. Jobs hand work to
each other only through recorded workloads, never through run artifacts.

Every run attempt writes one trigger record at
`trigger/<owner>/<repository>/<marker>/<run>-<attempt>.json`; a run is complete only
when every job succeeded or was skipped by plan.

## Release identity

Sources declare version `0.0.0`. A distributable binary is built unbound and receives
its release identity afterwards, so one built workload serves several markers. The
identity region format is owned here: `resources/identity/format.json` holds its
structure and `resources/identity/fixtures/` its shared evidence, which the writer
must reproduce byte for byte and every reader must test against.

- The region lives in one 4096-byte section with file content and no relocations;
  a signed PE input is refused.
- Section names stay within 8 bytes: the MSVC linker truncates longer PE section
  names, so `.releaseid` became `.release` in a real Windows image.
- An unbound region has payload length 0 and nothing after the length field.
- Binding an already bound region succeeds only with an identical binding and then
  changes nothing.
- The binding `digest` is `sha256` over the canonical release
  `{repository, marker, commit, tree}`; `workload` is the key of the unbound binary.
- A product with prefix `P` builds with `P_BUILD_TARGET` and `P_BUILD_CHANNEL=unbound`;
  such an executable refuses to run commands until bound.
