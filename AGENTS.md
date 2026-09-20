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
| `resources/` | non-code files, read only through `lib/content/resources.py` |
| `.githooks/pre-commit` | the only gate |

`scripts` may import `lib`; `lib` never imports `scripts`; scripts never import each
other; `lib` has no import cycles. Only `lib/content/resources.py` knows where files live.

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
- A script takes its parameters through `lib/parameters.py`, never from `argparse`
  defaults or `os.environ` directly. Each parameter declares its type once in
  `resources/parameters.json`; a value is taken from the flag, else `WHARF_<NAME>`,
  else the `-c <path>` TOML file, else the declared default, and a value set
  nowhere refuses naming all four. A value set but empty refuses too. Credentials
  never pass through this layer: a parameter or configuration key shaped like one
  refuses. Flags are for people and the environment is for CI, so a workflow `run:`
  stays a bare `python3 -B -m scripts.<name> <action>`.
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

The plan job decides, for every entry a run could hold, the workload key and
whether it runs, and records that as one document at
`plan/<owner>/<repository>/<marker>/<run>-<attempt>.json`, with a `latest.json`
pointer beside it. A job addresses its own run's plan from the context it
already has, reads its entry there rather than deciding again, and refuses if
the plan decided to skip it. It still resolves the basis it is about to record
and refuses when that resolves to a key other than the planned one, because a
workload record must hash to the key it is filed under. A job that consumes
another job's workload reads that key from the plan too, so a key is never
passed down twice.

The plan also tells the runner what to start: one matrix per target family and
one decision per single job, written to the runner's output file. Skipping is
absence from a matrix, not a condition on a job that exists. Whether a medium
is already published stays with the step that can ask its registry, because
asking needs that registry's credentials and one step holding all of them would
be the widest credential in the run; the plan records what those steps report.

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

## Depot

Depot generations are marker-bound blobs of one kind (`configuration`, `skill`,
`changelog`); wharf never reads their meaning. The standing generation on Depot is
the only source, its `previousGeneration` chain the history, and an edit pulls it,
changes it and publishes the next one.

- A new marker's base is its own standing generation, else the highest lower
  version on its `x.y.z` line; with neither, `--full` or `--from` is required.
- Unchanged objects are copied server-side, and the pointer is written only if it
  still holds the ETag it was read with.
- Only prerelease markers are written. Authors edit locally through the
  `wharf.depot` Runseal profile; CI inherits through `depot.yml`, which `ship.yml`
  calls per kind before `release` moves a channel pointer, so an installed binary
  always finds its generations.
