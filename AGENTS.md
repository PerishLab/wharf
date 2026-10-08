# wharf

wharf is the distribution hub of perish.code. It sits above Plumb: it distributes
Plumb, so Plumb does not govern it. This file is the only unstructured document in
the repository; `CLAUDE.md` only points here.

Read the canonical [PerishLab delivery governance](https://github.com/PerishLab/.github/blob/main/GOVERNANCE.md)
at work start and again before delivery or Issue closure. That document owns
organization-wide policy; this file owns Wharf's repository-specific exceptions.

## Shape

| Path | Holds |
| --- | --- |
| `.github/workflows/` | orchestration, one workflow per distribution path, and `manager.yml`, which rehearses the Windows manager |
| `.github/actions/` | composite actions, one directory each; `prepare` readies a machine for a layer unit |
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
- Words claimed elsewhere in perish.code are listed in `resources/check/vocabulary.json`
  and must not name anything here.
- A workflow `run:` is one `python3 -B -m scripts.<name> <action>` and nothing
  else. The action must be one that script takes, and every action a script
  takes must be run by some workflow, or be a layer unit `resources/units.json`
  registers; both are laws the gate enforces, so a path nothing reaches is a
  finding rather than a leftover. A step that prepares the machine rather than doing wharf's work
  — a registry login, a toolchain install — is listed verbatim in
  `resources/check/commands.json`, so shell can only enter by being written down.
- A script takes its parameters through `lib/parameters.py`, never from `argparse`
  defaults or `os.environ` directly. Each parameter declares its type once in
  `resources/parameters.json`; a value is taken from the flag, else `WHARF_<NAME>`,
  else the `-c <path>` TOML file, else the declared default, and a value set
  nowhere refuses naming all four, and a parameter no action takes is a finding.
  A value set but empty refuses too. Credentials
  never pass through this layer: a parameter or configuration key shaped like one
  refuses. Flags are for people and the environment is for CI. The same layer
  carries the answer back: what a script tells the runner it decided goes
  through `parameters.answer`, not through shell reading the script's output.
- A release marker has one shape, owned by `lib/content/marker.py`, which also
  says what channel a marker is on and how two markers order. Nothing else
  matches a marker with a pattern of its own.
- A command whose output a step needs back is captured; a command that takes
  minutes streams to the log while it runs, because a step that prints nothing
  until it ends cannot be told from one that hung, and a failure that prints
  only afterwards has already cost the wait. What a script decided still goes
  back through `lib/parameters.py`; the runner's output file carries decisions,
  never logs.
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

Rust, Node and pnpm versions are the domain's, not the product's: the plan job
installs the latest plumb stable, reads `plumb metadata --json` once, and hands
the result to every job that builds, tests, deploys or publishes as
`WHARF_DOMAIN`. A product's `rust-toolchain.toml` and `engines` are not read,
and a job without `WHARF_DOMAIN` refuses. Rust installs at that version with the
`minimal` profile, so the toolchain in a basis is the domain's.

The dependencies a binary compiles against are a workload of their own. Their
basis is the binary's basis without the members' own sources — the member
manifests, the lock closure, the toolchain, the target and the runner — so a
product's own change does not move them, and two products that lock the same
closure share one record. The build job restores them when the plan already
holds them and records them when it does not, from the same build either way.
What is recorded is the cargo target directory with every artifact of a
workspace member and cargo's own probe cache left out, so nothing a product
builds itself can come back stale. It is packed under one fixed timestamp to
stay byte for byte the same, and restored under one instant, because cargo
rebuilds whatever it finds an input newer than.

The plan also tells the runner what to start, written to the runner's output
file. Each entry records the entries whose keys its basis names as `consumes`,
and the plan derives layers from that: an entry sits one layer after the
deepest entry it consumes, while what publishes comes after every layer, since
publishing is ordered by policy and never written into a basis. A layer is one
matrix of units, each naming its action, runner and the preparations
`.github/actions/prepare` makes before it; `resources/units.json` says which
entries a unit covers, and the workflow runs a fixed number of layers. What
publishes still has a job of its own and one decision each; every one waits for
all the layers. Release, which writes the seal, is one medium among them, and
channel, which moves the pointer people install from, waits for every medium,
so a seal says only that its binaries are published; how far the marker is
distributed is the distribution record's to say. A product whose `plumb.toml`
declares binaries builds, binds and releases them for exactly the targets its
`[release].targets` lists, which must include the primary Linux target smoke
and validation run on, and its managers offer only those. The targets wharf
releases are `x86_64-unknown-linux-gnu`, `aarch64-apple-darwin` and
`x86_64-pc-windows-msvc`; plumb accepts exactly the same set, and a test on each
side pins it, so the set changes in both together. A product that declares no binaries has none of that: no binary, bind, smoke, validate
or deb entry, no seal and no channel pointer. Its declared OCI attachment builds
from the tracked source context and proves the public repository digest instead of
inventing an executable. Binaries and channel are recorded none, and the
distribution record alone says the marker is distributed. Suites follow
the source that is there: a tracked `Cargo.toml` plans the cargo suite and a
tracked root `package.json` the node suite, and neither is planned without it. The product's
`plumb.toml` is likewise the one declaration of what else publishes: npm publishes
exactly the packages `[release.npm]` lists, cargo exactly the crates
`[release.cargo]` lists, chart exactly the chart `[release.chart]` names and
oci an image only where `[release.oci]` is declared, each refusing a listed name its workspace does not answer
or will not publish there; a package or crate it does not list is never published,
and native files keep only the configuration their ecosystem reads, such as the
`.npmrc` scope map and the cargo registry index. Before npm publishes anything it
packs every package it is about to publish and refuses them all if one names a
file it does not carry — an entry in `exports`, `main`, `types`, `svelte` or
`bin`, or a relative import inside a packed script — because a registry version
cannot be taken back and a package that installs but does not resolve is worse
than one that never went out. Channel decides for itself: it
runs unless the pointer already names a newer marker, so a seal whose pointer
never moved is pointed at by the next run instead of being skipped with it.
Skipping is absence from a matrix, not a condition on a job that exists. A
single job that covers several targets takes them from the plan it already reads, and refuses
when the plan decided none of them. Whether a medium is already published stays
with the step that can ask its registry, because asking needs that registry's
credentials and one step holding all of them would be the widest credential in
the run; the plan records what those steps report.

A release is one marker and one seal over every executable `[release].binaries`
lists. `[release.binary.<name>]` may narrow an executable to a subset of
`[release].targets` or set `install = false`. A target is still one binary
workload, built by one cargo build of every executable that declares it, so no
entry is named after an executable; each carries the product's identity
prefix, is bound as the product, and smokes as `<executable> <marker>`. A
platform archive holds that platform's installed executables and the managers
install exactly those; one not installed still builds, binds and smokes, and
reaches people only through a placement. A binary-backed placement names the
executable it carries, else the one named like the product, else the first:
`[release.oci]` builds its image from it and runs the image with `--version`,
entrypoint kept, refusing before the push unless it reports `<executable>
<marker>`; an attachment-only `[release.oci]` instead builds the committed
source context and refuses unless an anonymous client resolves the pushed tag
to the digest the local image reports. The Linux
binary is built on `ubuntu-24.04`, so an image base must provide at least its
glibc, 2.39. `[release.deb]` packs it at
`/usr/bin/<executable>` with its declared root into
`<Package>_<Version>_amd64.deb`, named after the executable. wharf owns that package's `Version`
(`vX.Y.Z-rc.N` is `X.Y.Z~rc.N`), `Architecture` and `conffiles`, which list
everything under `/etc`, and packs it at `SOURCE_DATE_EPOCH=0` with one
compressor thread, the fixed instant every archive here uses, so a rerun
repeats the bytes. `[verify] deb` installs it with a strict `dpkg -i` in the
`ubuntu:24.04` image `resources/build.json` pins by digest, after only the
`Depends` it declares, and checks its version, its files, its units and the
marker its executable reports, starting nothing. It enters the seal as
`linux-x64-deb`, written by `[publish] binaries` with the credentials that job
already holds, which refuses a declared deb whose verification is not
recorded. wharf publishes placements; it never deploys them, and keeps no apt
repository: a host takes the deb by reading `v1/channels/<channel>.json`, the
seal it names, and that seal's `artifacts.linux-x64-deb` URL. A product
carries at most one deb and one image, the image named after the repository
whatever executable it carries, and the deb is built for amd64 alone; a second
server placement or architecture is a new declaration, not a second table
today. plumb is the one gate on a declaration's shape, so wharf does not
repeat its checks, such as the systemd unit a deb must carry.

A job that consumes another job's workload names every job that produced one in
its `needs`, not only the last of them. A job the plan left nothing to do and a
job whose own condition was false because an ancestor failed both read as
skipped, so a gate that admits skipped ancestors admits a failed build unless
each producer is named.

A run is complete when every job succeeded or was skipped by plan, and that is
what the distribution record carries; the run's own page keeps what each job
did, so wharf writes no second record of its own.

Tracked worker configurations under `[preview.app.<name>].path` are preview-only:
ordinary worker discovery validates their declaration and excludes them from ship.
A preview target must not share its account and Worker name with any other tracked
configuration. Invalid or missing preview paths refuse before install or deployment.
Preview execution is a separate path, never release distribution.

`lib/content/static/admission.py` loads current registration only from a trusted
store and observes the fixed main Preview workflow and both actors' repository
push permissions through the read-only GitHub adapter. Apply also verifies the
remote exact commit/tree before fresh checkout qualification; inspect/discard
do not require product source. Trusted platform context, store and API credentials
must stay outside product/request inputs and guests. The returned observation is
not a bearer permit, source acquisition or a provider-write/recovery authorization.
Workflow integration must revalidate current admission before external mutations.

`.github/actions/preview-admission` prepares the workflow-facing read-only entry.
Bounded request JSON selects neither platform identity nor credentials/store origin.
Runtime context must be the exact main Preview workflow and is independently
checked against GitHub. Separate control/product credentials are consumed only
by the trusted admission step. `lib/store/r2.py` exposes a bounded GET-only
registration reader for one repository/app key, with no writer/store fallback.
Entry selectors and admission digests are observations, not bearer permits,
acquisition receipts, deployment results or quiescence evidence. The future
workflow must exclude these readers from product guests and revalidate admission
before mutation; this component alone opens no dispatchable Preview workflow.

Preview request, registration and result envelopes are explicit versioned contracts
in `lib/content/preview.py`. Caller and workflow fields are observations, not
authorization; trusted onboarding and the execution adapter must establish authority.
`lib/store/preview.py` coordinates exact requests with versioned snapshots and
conditional writes under `preview/v1/`, never the release namespace. Unknown
operations retain their reservation without lease expiry or automatic takeover.
Same-request readback is continuation evidence, never permission for a second
provider write; the adapter must establish original-run quiescence before recovery.
Discard advances the revision and retains a tombstone; an old apply cannot recreate
it. A degraded upload preserves the last verified deployment as historical evidence,
not as a claim about the latest URL. These modules do not build, upload, provision,
prove content or delete provider objects; the dedicated workflow owns those actions.

Static qualification and output validation live in `lib/content/static/`.
`lib/media/node.py` builds only an exact clean checkout and registered static app,
using prepared tools outside product source and an allowlisted child environment.
Script-disabled installation alone may hold the package reader; full/head Guard
and build hold neither that reader nor provider/storage credentials. Tool/source
changes refuse. These child boundaries are not a filesystem sandbox: workflow
integration must keep write credentials entirely outside the product build job.
`lib/store/handoff.py` records vetted bytes, source manifest and fixed no-script,
no-external-content/no-store headers as an immutable verified content workload.
Its key binds exact source, Guard, tools and implementation, not environment names
or request IDs. Reuse is content evidence, never deployment authorization.

`lib/content/static/runtime.py` owns the bounded local Preview guest primitive.
Only disposable temporary product directories and separate read-only trusted
controls are mounted. The public Images stable reference and offline profile
live in `resources/build.json`; no host credentials, home or Docker socket enter
the guest. Stdout alone is returned; stderr remains diagnostic. Both streams are
drained live without a TTY under one aggregate output budget, deadline and
workflow-command suppression. Stream separation is not proof authentication;
product-authored stdout remains untrusted. Every exit path reconciles only the
invocation-labelled container, removes it and independently confirms absence;
failed or unknown teardown admits no output. Client timeout is not cancellation.
Stable is prepared first; the guest runs only its resolved digest, recorded in
host-observed identity, never a guest-authored Guard proof.
This primitive grants no source/target authority, package reader or publication.
`lib/content/static/bridge.py` binds this runtime to offline static content through
separate install, full head Guard and build guests. Each guest must disappear
before the next phase or byte collection. Source, Git control metadata, trusted
controls, tools and host-observed runtime identity are checked across phases.
Unknown teardown retains the disposable workspace and admits no handoff.
The isolated receipt binds this execution world and is independently checked by
the content consumer. It does not authenticate arbitrary product-authored proof.
The bridge grants no package reader, provider publication or workflow admission;
source/caller/target authentication remains a prerequisite for workflow wiring.

`lib/content/static/workspace.py` stages qualified exact source into a disposable
independent Git checkout, never a writable mount of a canonical worktree.
Acquisition readback admits real absolute independent checkouts in the trusted
runner workspace; only separately staged guest mounts require temporary placement.
The source entry preserves paths for qualification rather than resolving aliases.
Prepared system Git uses an isolated home and fixed control configuration; source
hooks, local configuration, ignored files and shared object storage are not copied.
The supervisor must authenticate source acquisition first and confirm every guest
is gone before closing this workspace lifetime. Staging alone proves neither
Guard authority nor complete build isolation and does not limit history disk use.

`lib/check/dependency.py` qualifies only a bounded literal workspace
packages list and package JSON configuration subset. Unsupported settings,
runtime selectors and dependency protocols refuse. Its reader projection holds
only fixed registry routing and an empty workspace, never manifests or scripts.
Input digests identify provided bytes, not completeness or authenticated source.
A trusted caller must bind the complete selected source separately. Qualification
grants no reader authorization: lockfile/integrity/URL validation, isolated reader
network and teardown, verification-cache transport and live workflow integration
remain separate obligations. Ordinary static builds do not yet invoke this module.

`lib/check/locking/` composes that projection with bounded, ASCII literal pnpm v9
lockfile qualification. Unknown fields, ambiguous YAML, unsupported identities,
dangling package/snapshot edges, manifest/importer disagreement, foreign protocols
and noncanonical download routing refuse before reader credentials are considered.
Every package resolution requires canonical SHA512 integrity, including unused
optional records. Public tarballs bind exact package/version; first-party GitHub
tarballs bind the declared scope, package, version and opaque identifier. These
checks do not prove provenance, latest-stable authority or redirect confinement.
The projection retains exact qualified lockfile bytes and input digests alongside
fixed credential-free configuration, not product manifests, scripts or runtime
selectors. A trusted caller still authenticates and supplies the complete source.
Reader network/redirect isolation and teardown, store/cache verification transport,
latest-stable first-party Guard authority, authsource and official live Preview
integration remain separate obligations under #82 and #36. Ordinary static builds
do not yet invoke this module; qualification grants no reader authorization.

`lib/check/locking/checkout.py` derives the complete selected dependency inputs
from the declared exact Git tree after independent acquisition readback. Root and
every admitted workspace manifest are selected from a bounded NUL-delimited tree
inventory, never a caller-provided subset. Original immutable blobs are bounded
and independently hash-checked; no worktree path, filter, hook or product script
is used to read their content. Acquisition is rechecked before returning the
source-bound credential-free projection. Unsupported selectors and ambiguous
selected paths refuse. This binds repository content identity, not authenticated
caller authority or hostile concurrent-writer isolation. Existing acquisition
history/checkout checks are reused without a new total disk-bound promise.
The offline bridge does not invoke this collector yet. Reader authorization,
network/redirect/teardown, store/cache transport, latest-stable first-party Guard,
authsource and official-action/live obligations remain under #82 and #36.

How far a marker is distributed has one authority: `distribution.json` beside its
seal on the product's release authority, at
`v1/releases/<channel>/<marker>/distribution.json`. It records the marker's
commit and tree, what became of each medium — binaries, npm, oci, chart, cargo,
cfworker and the channel — and whether some run completed it. A medium this run
carried out is published, deployed or pointed; one the plan left alone is
present when it is already there, none when the product has no such medium, and
overtaken when the channel already names a newer marker, as each deciding step
reports it; failed, cancelled and unreached say what stopped the rest. Every attempt
merges into it and nothing moves back: a medium once published stays
published, a completed marker stays complete, and a marker recorded at another
commit refuses. Plumb reads it; the seal, the channel pointer, the registries
and GitHub's own verdict are evidence, and where they drift
from it, a new dispatch brings them back rather than anyone editing it.
`[record] distribution` writes it with the release bucket's credentials alone,
and nothing else in the run holds both those and the workload bucket's.

## Release snapshot

A release plan clones the immutable marker into an owned disposable checkout,
resolves first-party stable packages with released `plumb lift`, and runs actual
full/head Guard there. Its record keeps the original marker identity, effective
Git tree, exact package set, control authorities and the genuine Guard result.
Builds recreate that exact combination and refuse movement. Compilation uses the
fixed sibling directory declared by `resources/build.json`; the layout is part
of Cargo workload implementation identity. An existing destination refuses.
Source checkouts stay unchanged and disposable directories are removed on exit.

npm builds and lifecycle scripts run while packing a workload. Publication consumes
those vetted archives with lifecycle scripts disabled. Before any medium writes,
Wharf rechecks current stable resolution, confirms source and tools unchanged,
and reserves the marker's exact combination. Distribution records attribute the
combination only to successful publication jobs. Historical publication without
that evidence remains unknown; partial recovery refuses to mix combinations.

## Follow

The main Follow workflow accepts only authenticated `follow-push` dispatches.
`lib/process/follow/receiver.py` exposes a WSGI application: its host supplies the
GitHub App webhook secret and a separate `WHARF_FOLLOW_DISPATCH_TOKEN` restricted
to Wharf Contents write, enforces a 25 MiB body limit and a finite request/read
deadline, and retains failed GitHub delivery identities for explicit replay.
The receiver verifies the original webhook HMAC and signs a bounded push summary
with a separate purpose prefix. It holds no App private key. The workflow verifies
that signature, the configured dispatch sender, main runtime and minted installation
before qualifying current repository identity and its root Plumb declaration.

Configure `WHARF_FOLLOW_APP_CLIENT_ID`, `WHARF_FOLLOW_SENDER_ID`,
`WHARF_FOLLOW_RUNNER` and `WHARF_FOLLOW_DATA_ROOT` as trusted Wharf variables;
the runner value is a JSON label list selecting one persistent Linux runner.
`WHARF_FOLLOW_APP_PRIVATE_KEY` is the dedicated follow App secret and appears only
in this workflow. `WHARF_FOLLOW_WEBHOOK_SECRET` matches the receiver's webhook
secret; package resolution reads `PERISHLAB_PACKAGES_READ`. The App token is scoped
to the signed repository and exactly Contents, Pull requests and Issues write plus
Metadata read. Read back the actual registration, push subscription and installations
before treating runtime setup as complete.

Follow data lives outside the runner's disposable workspace and temporary directory.
Wharf creates an owned canonical main checkout per repository and one persistent
Plumb home; it preserves the common Git directory, Auto worktrees and released
Plumb recovery records between runs. Foreign, dirty or diverged source refuses.
Only released `plumb follow` owns Auto issue/pull/proof/recovery/closure. The workflow
serializes each signed repository, uses a finite process budget and terminates its
command group on exit. A current consumer creates nothing. No schedule or RC gate
backs up this path.

## Names

Every name a person reads on GitHub — a run's title, a job's, a step's — takes
one form, `[<verb>] <object> [<qualifier>…]`, so the bracketed column says what
happens and the rest says to what. A qualifier only tells instances of one kind
apart: a target, a runner, a shell, a repository and marker, a Depot kind. A layer unit's
name is rendered from the verb and object `resources/units.json` gives it.

| | Within wharf |
| --- | --- |
| Verbs | ship, lodge, follow, plan, build, test, bind, smoke, validate, publish, deploy, point, record, checkout, verify, identify, prepare, login, read, decide |
| Objects | entries, binary, binaries, deb, suite, npm, oci, chart, cargo, cfworker, channel, distribution, plan, wharf, product, request, source, registry, installation, engines, machine, autocrlf, node, pnpm, plumb, manager, and the Depot kinds |

A name that cannot be written as one verb over one object marks a job doing two
things; that is how writing the seal and moving the channel came apart.

One name escapes the form: a layer whose matrix is empty is skipped before its
matrix expands, and GitHub does not evaluate the name of a skipped job, so it
shows `matrix.name`. No expression in that name can help, and a placeholder
unit would break skipping as absence, so it stays.

## What a release keeps

These were Plumb's laws while Plumb shipped; they are kept here, beside the code
that keeps them.

- A medium is published by an explicit marker dispatch and never by a landing.
- Every object is addressed by its digest and written create-only: an existing
  key is left alone when the bytes match and refuses when they differ, so a
  release is immutable and a rerun is safe.
- A seal is read back through the product's public authority after it is
  written, and a release that is not served as written refuses.
- A seal of a product `resources/releases.json` names under `guard` — plumb
  alone — carries `guard: {producer, depot}`, the authority its bound Linux
  binary reports with `release authority --json`, run by `[publish] binaries`
  in a clean home with no credential in its environment. It must hold exactly
  those keys, a producer `<marker>@<commit>` naming this release's marker and
  commit, and a depot of 64 lowercase hex, or the release refuses; so does a
  binary that lacks the command. The binary is asked only when the seal is
  about to be written, before any object is, so rerunning a release whose seal
  exists answers already published without it. It is written on every channel, and only a
  consumer holds it to stable. Any other product runs nothing and its seal
  keeps its bytes, which a test pins.
- Managers, seals and channel pointers are rendered by the run that publishes
  them; no product carries them in its source.
- The canonical managers at the authority's root are written only when a stable
  channel pointer names the marker that rendered them.
- Each job holds the credentials of one bucket, and nothing in a run holds two.
- A job reads the product through `.github/actions/product`. The Wharf GitHub
  App (`WHARF_APP_CLIENT_ID`, `WHARF_APP_PRIVATE_KEY`) is installed on every
  repository of the organization, so onboarding a product needs no App step;
  `secrets` holds no plaintext key material, so the wider reach adds little.
  A product is onboarded by its `[release]` declaration and its releases and
  depot buckets, not by the installation. The action always mints a token
  scoped to the one repository being released, contents read alone, for that
  job, and checks out without persisting it. A mint failure stops the job
  because the App is broken or the repository is outside the organization.
  This reads source and writes nothing, so it is not a bucket credential.
- A job that installs a pnpm workspace — a layer unit and the worker deploy —
  holds the run's own token with packages read alone, as `WHARF_PACKAGES_TOKEN`,
  because GitHub Packages answers no anonymous install, not even of a public
  package. It reaches `pnpm install` through a user npmrc that names it and is
  gone before the product's tests run. It reads packages and writes nothing, so
  it is not a bucket credential either.

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
  such an executable refuses to run commands until bound. Every executable of a
  release takes the product's prefix and is bound with the product's name, not
  its own.
- Binding reads and writes the region as file content and never executes the binary,
  so one Linux job binds every target the plan decided to run. Smoke stays on the
  target platform because it runs the bound binary. The binding basis names the
  unbound binary and the release, never the runner, so where binding happens does
  not change its workload key.
- Editing a Mach-O invalidates its signature, and an arm64 image with no valid
  signature does not run, so binding one signs it ad hoc afterwards. The signer is
  a pinned `rcodesign`, which signs the same way on every platform; one signer is
  what keeps a key's bytes reproducible, so nothing else may sign. Its own verify
  reports itself as unreliable and is not used: what proves a signature is smoke,
  which runs the bound binary on the platform it was built for.

## Managers

A release carries the scripts that install it, and this repository renders them:
`resources/manager/` holds one template per platform, filled with what the
release already knows — the product, its authority, the platforms this
repository publishes and the version being released. A stable release renders a
second pair with no version pinned, which is what the authority serves at its
root, and whose moving intent is stable alone.

Rendering belongs here because the scripts are clients of the layout here: they
read the channel pointer, the seal and the objects by the names
`resources/releases.json` gives them. It belongs here for a second reason — the
generator a seal names covers the implementation and the resources it read, so
a script rendered anywhere else would be published under a claim that does not
reach it.

One product is named in the templates. When the product is `plumb`, both
managers run `plumb configuration install` after installing it, so a fresh
Plumb projects its configuration at once; Plumb declares the same step on its
side. No other product is special-cased.

The Windows manager is proven by running it, not by reading it. `manager.yml`
runs `[test] manager windows` on `windows-2025`, once under `pwsh` and once
under Windows PowerShell, whenever a change reaches the manager or what it
reads, and on dispatch. It builds the fixture `resources/manager/` declares — a
product `demo` with two installed executables and one `install = false` — as
tiny executables that report `<executable> <marker>`, and publishes two stable
markers with the same `render` and `publish` a release uses into a local
directory. A loopback server stands in for the authority, naming itself in
every JSON it serves where the seal names the real one. Through it the pinned
managers install, reinstall, update, uninstall a version, and the canonical
manager installs and uninstalls everything, each step checked against the
exact files, versions and seats it must leave. It publishes nothing and holds
no credential.

## Cargo

The perish cargo registry is a static sparse index on R2: `perish-cargo`, served
at `https://cargo.perish.uk/`, which `resources/registries.json` names. Cargo reads
it with no API at all — `config.json`, one index file per crate, and the `.crate`
blobs — so nothing answers requests and nothing but a bucket can fail.

- `[publish] cargo` packages each pending crate with `cargo package`, derives its
  index line from the manifest the `.crate` carries, and writes the blob where
  the served `config.json` says it is downloaded from. The key names the blob's
  `sha256`, and it is created only.
- The index file is appended only if it still holds the ETag it was read with,
  and a version it already lists refuses: a published version never changes.
- A dependency on the registry itself is recorded with no registry, so the index
  carries no identity of its own and moves between hosts unchanged.
- The job holds the writer of `perish-cargo` alone.

## Depot

Depot generations are marker-bound blobs of one kind (`skill`, `changelog`);
wharf reads their meaning only to vet what it lodges. The standing generation on
Depot is the only source, and its `previousGeneration` chain is the history.

- Everything enters through the yard: `perish-wharf-yard`, a private bucket
  whose objects expire after seven days. Plumb consigns one JSON document per
  consignment at `<owner>/<repository>/<marker>/<kind>/<digest>.json`, created
  only, carrying each object's path, digest, mode and body. The yard's writer
  reaches no Depot bucket.
- `depot.yml` lodges one consignment. It reads it by the digest its bytes must
  hash to, vets it — the latest plumb stable proves a changelog again against the product
  at its marker, and a skill must carry a `SKILL.md` within plumb's wayfinder
  cap, since skills are written for their release and no product tree carries
  one — and only then writes the generation. The yard is an untrusted inbox, and expiry
  is a floor rather than a promise, so lodge checks presence and digest every
  time instead of trusting either.
- A lodged generation's base is its own standing generation, else the highest
  lower version on its `x.y.z` line, else the highest lower stable; with none,
  it starts the product's lineage.
- Unchanged objects are copied server-side, and the pointer is written only if
  it still holds the ETag it was read with.
- `depot.yml` is Depot's only writer; nothing lodges from a workstation. Ship
  never calls it: the two paths hold different credentials and answer to
  different lifetimes.
