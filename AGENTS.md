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
| `tests/` | tests mirroring `lib/` and `scripts/`, with native provider protocol tests under `tests/cfworker/` |
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
tracked root `package.json` the node suite, and neither is planned without it.
The cargo suite is not planned either when the plan's full Guard for this
combination succeeded with a `guard/test` action and the root `Cargo.toml` is a
virtual workspace without `default-members`: Guard then ran `cargo test --locked`
over every member, the tests the suite would repeat. Guard evidence names actions,
not their commands, so that command is pinned by a test on each side
(`resources/identity/coverage.json` here, PerishLab/plumb#229 there), and the two
change together. The product's
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
than one that never went out. The `source` export condition is reserved for
development inside the product's repository, where tests resolve a package's
own name to its sources; packing removes it from `exports`, so a published
package names and carries only its build, and a subpath that only `source`
serves refuses. Channel decides for itself: it
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

Tracked Worker configurations under `[lane.app.<name>].path` are development-only.
Ordinary discovery reads explicit repository/app identities, default or quoted
keyed Preview bindings, capabilities, independent authority roles and cfworker
account/resource mappings. Retired `[preview]` declarations refuse without aliases.
The package selector and source Worker must match the declaration; these files
must be tracked regular files without symlink ancestry. Invalid, missing or shared
targets refuse before install or deployment, and qualified development mappings
are excluded from ship. Multiple bindings of one app may share its declared parent;
separate tracked configurations may not share a development target. These source
judgments prove neither trusted registration nor authorization. Preview execution
remains separate from release distribution; runtime envelopes, lane state and
public evidence publication are separate consumer work under #121.

`lib/content/lane/` reads and encodes the shared lane identity, artifact,
declaration, context, exact selection, operation and reference contracts. Ordered fields,
capabilities and reasons are carried in `resources/identity/lane.json`; the byte
and digest fixtures are observations from the public Rust types at the recorded
Plumb source, not a runtime version selector. Encoding follows typed Serde order,
not the sorted-key release codec. Readers require JSON objects, reject unknown or
duplicate fields and return detached values; encoders validate again before use.
Optional artifact build identity becomes explicit null. Supplied selection
readback must match its authority, digest and exact canonical record bytes.
These checks prove neither publication, authorization nor retention. Static runtime
consumes shared state below; no legacy Preview envelope is treated as an
operation record. Generic families and distribution actions remain
separate from the static Preview declaration's narrower capability requirements.

Operation requests bind their canonical byte digest to an assessment and require
action-specific explicit state preconditions. Admission keeps unsupported,
denied, conflict, unavailable and unverified judgments distinct. Declaration
binding additionally checks exact target/adaptor, capabilities and authorization
authority. Applied mutations require an idle writer and the exact intended
material; refusal and unknown outcomes preserve the admission reason. Canonical
operation readback can settle only an applied observation of the exact request.
These judgments validate supplied observations; they authenticate no authority,
grant no provider write, mint no bearer permit and prove no public publication.
The generic supplied-byte codec does not bind a declaration's publication
authority on its own; trusted public composition below supplies that binding.
Standalone State and Outcome readers mirror raw Rust wire forms; Request,
Conditions and Operation enforce their contextual invariants before consumption.

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
in `lib/content/lane/preview.py`. Caller and workflow fields are observations, not
authorization; trusted onboarding and the execution adapter must establish authority.
`lib/store/lane/preview.py` coordinates exact requests with versioned snapshots and
conditional writes under `preview/v1/`, never the release namespace. Unknown
operations retain their reservation without lease expiry or automatic takeover.
Same-request readback is continuation evidence, never permission for a second
provider write; the adapter must establish original-run quiescence before recovery.
Discard advances the revision and retains a tombstone; an old apply cannot recreate
it. A degraded upload preserves the last verified deployment as historical evidence,
not as a claim about the latest URL. These modules do not build, upload, provision,
prove content or delete provider objects; the dedicated workflow owns those actions.

`lib/store/lane/registration.py` and `state.py` consume the shared lane contract
under `lane/v1/<repository>/<app>/<lane>/`, separately from release and historical
Preview objects. Exact static registrations are versioned trusted-store documents;
state pins their complete digest, never silently resetting after registration
movement. Native snapshot/CAS conflicts propagate without retry or takeover.
Requests bind exact revision, request identity, declaration admission and observed
material. Same-request readback is continuation evidence, not another execution
permit. Unknown and refused results retain reservations: a shared refusal alone
does not prove writer quiescence. Only an admitted exact applied operation with
idle writer advances revision; disposal retains absent material and history as a
tombstone. These are supplied observations, not authenticated provider/public
evidence. Registration and current state are separate conditional objects, not
a multi-object transaction or atomic authorization guarantee. Trusted workflow
composition must revalidate registration before external mutation. This source
kernel invokes no provider, credentials, workflow, repair or installed lane CLI.

`lib/store/lane/public.py` consumes an independently prepared publication
authority and exact bare HTTPS DNS origin. Anonymous GET reads only
`lane/v1/evidence/<record-digest>.json`, with a 64 KiB document budget and
30-second network idle timeout. It forwards no credentials and accepts no
record-selected origin, redirects, content transformation or private fallback.
`publication.py` validates the shared finite public record, exact declaration
target/adaptor and operation capability/authorization roles before create-only
publication. Existing keys must hold identical private bytes and still pass
fresh independent public readback. Digest and canonical bytes, the entire
requested context and the prepared declaration publication authority must agree.
Refused/unknown evidence can be published but cannot settle a request; only an
exact applied operation also admitted by its declaration can settle. Returned
observations are detached data, not provider authorization or a bearer permit.
No mutable pointer, current-state update or provider execution is performed.
Trusted onboarding owns the origin/writer mapping, DNS/public reachability,
resource exclusivity and retention; this source kernel proves none of those
live facts. Workflow composition and physical provider proof remain separate.

`lib/process/cfworker/observation.py` projects parent, Preview and exact deployment
identity from bounded provider replies. The caller supplies trusted onboarding
pins for the immutable parent identity and account subdomain; readback never
creates or repairs those pins. This narrow dedicated-parent policy requires
disabled ordinary workers.dev routing, enabled Preview routing, no parent
deployment, no Preview base configuration and empty reported resource references.
It is an adaptor precondition, not proof of complete production isolation.
`Reader.observe` binds the selected Preview name, slug and single native URL to
the prepared parent, then binds the exact deployment to that Preview identity and
rereads the parent policy. Failure refuses without proving absence. The returned
projection excludes arbitrary bindings, annotations and deployment URLs. It
proves neither vetted content, origin reachability, atomic observation nor writer
authorization; parent reread does not eliminate concurrent provider changes.
The admitted opaque parent/Preview identity syntax and UUID deployment selector
are deliberately narrow supported subsets, not a claim about all provider IDs.

`lib/process/cfworker/mapping.py` maps admitted static registrations to native
Worker Preview names, keeping parent/account and exact logical lane distinct.
Default `preview` and keyed `preview.<key>` map injectively without truncation;
the combined Preview-parent DNS label must fit 63 bytes. A supplied prepared
catalog refuses repeated logical or physical targets, but does not prove catalog
completeness or onboarding resource exclusivity. `reader.py` uses a prepared
read-only token with proxy-free fixed HTTPS GETs for that parent, Preview and an
exact deployment UUID, never latest, source configuration or an arbitrary URL.
Redirects, transformed/oversized/duplicate/malformed replies and transport or
cleanup failures refuse; an HTTP failure is not verified absence. Successful
object replies are raw trusted-provider observations, not validated deployment
identity, public evidence, admission or a bearer permit. No create/upload/delete,
parent provisioning, product command or automatic quota-eviction handling is
implemented by this bounded source component. These remain later #122 work.

`lib/process/cfworker/inventory.py` reads a bounded complete native Preview catalog
through fixed page-number GETs. Page/count/total metadata, stable pagination and
unique names/identities are required; incomplete, oversized or malformed catalogs
refuse. `Reader.status` validates prepared parent policy, projects the selected
Preview, directly binds a present selection and independently repeats the entire
catalog and parent observations. Only complete successful listings establish a
selected Preview's observed control-plane absence; HTTP errors never do. Successful
envelopes require explicit empty or null errors. Projections omit arbitrary private
fields. Repeated agreement is not an atomic provider snapshot, original-writer
quiescence, URL teardown, content integrity, quota safety or mutation authority.
Exact deployment UUID observation remains separate; no latest selector, checkout,
writer operation, automatic recovery or installed status command is supplied here.

`lib/process/cfworker/discard.py` binds supplied complete status projections to
an exact registered dispose reservation and prepared parent identity. Its finite
plan is detached data, not a bearer permit or authenticated observation.
`writer.py` separately holds the prepared DELETE credential and binds one immutable
selected Preview ID, refusing ambiguous mutable names and unknown recovery writes.
An attempted writer is consumed even on response loss; no retry or parent/sibling
endpoint exists. A lock serializes one instance, not durable cross-instance custody.
A successful acknowledgement does not prove absence. Supplied
settlement requires selected absence, unchanged parent/sibling identity projections
and explicit idle original-writer observation; it proves neither public URL teardown
nor sibling content preservation. Trusted workflow composition must authenticate
observations, establish original-writer quiescence and revalidate current admission,
registration and reservation before mutation, then bind public evidence and state.
No such workflow, authenticated recovery, actual provider write or CLI is supplied
by these source primitives; repeated status is not an atomic authorization check.

`discard.resume` binds read-only continuation to the original unknown dispose,
unchanged registration, revision, active request/assessment and historical material.
It grants no new DELETE attempt; a refused result or elapsed timeout is not recovery
authority. `unknown` returns shared unavailable/unverified operations, retaining the
reservation. `published` requires independent public applied absence for the entire
original request and assessment before returning an eligible shared operation.
These validate supplied facts, not live absence, writer idleness or execution order.

`reader.inspection` binds an inspect request and assessment to the exact registered
current revision and settled material without reserving or advancing current state.
Busy/unknown mutations and uncertain writer observations refuse verified inspection;
historical material cannot stand for current routing. `Inspection` consumes complete
status and, for presence, saved immutable vetted handoff bytes without a product
checkout, build, script or writer credential. Full artifact/source/app/generation
identity and every canonical asset's exact bytes/media/security policy must agree
at the prepared mutable lane origin. Handoff bytes are checked before and after.
Selected control-plane absence with settled absent material consumes no content and
does no content HTTP read; it does not prove URL teardown. Finite applied inspection
uses the shared operation contract and exposes no private provider fields or UUID
selection claim. `reader.published` binds its entire exact request and assessment to
independent public readback with idle writer. Trusted workflow owns current admission,
authenticated complete status, original-writer quiescence, registration/state reread
and public evidence sequencing; these reads are not an atomic provider snapshot.

`lib/process/cfworker/apply.py` binds one reserved deploy request to a verified
immutable handoff, exact registered repository/app/configuration generation and
full content/source/build identity. Static artifact build inputs name the content
workload key; build world hashes its prepared tools and implementation projection.
Every payload serialization revalidates the exact handoff bytes and current trusted
implementation. Unknown reservations refuse new write payloads. Supplied plans
authenticate neither acquisition nor admission and grant no provider write.
`payload.py` prepares native asset manifests using the official direct-upload
SHA-256 example over base64 bytes plus extension, truncated to 32 hex characters.
That provider cache identifier is not the full content digest or SDK BLAKE3 byte
identity; differing byte/extension collisions refuse. The identity file stays an
asset; fixed vetted `_headers` bytes enter a separate multipart deployment file.
Assets-only metadata has fixed routing and no main module, bindings or migrations.
Its completion JWT and serialized multipart bytes are private transport material,
never plan or public evidence. Their shape grants no completion authority or proof
of uploaded content. Provider upload/create/deploy, capacity/eviction checks,
authenticated custody/recovery, exact deployment/content readback and immutable
public result binding remain later work; this preparation performs no network I/O.

`lib/process/cfworker/upload.py` holds separately prepared asset-session custody
and emits fixed proxy-free POSTs only to the registered parent's asset-session
and account bulk-upload endpoints. Every request revalidates the exact plan and
immutable handoff. Provider buckets must select unique admitted cache identities;
the entire catalog is checked before upload custody is accepted. Multipart uses
base64 bytes and fixed static media types, never product code or configuration.
Session and bucket attempts are consumed before network I/O; ambiguous replies
block further uploads and completion, without retries or fallback. Workflow owns
bucket scheduling. A lock serializes one instance; this is not durable admission,
cross-instance exactly-once execution or original-writer quiescence. Finite results
omit private JWTs; the explicit completion accessor returns private transport
material only after all required acknowledgements and fresh byte verification.
Only successful HTTP 200 non-final bulk replies admit an absent, null or empty
result. Session replies still require their exact JWT/bucket object, and final
completion still requires HTTP 201 with exactly a verified private JWT at its
proper bucket position. This endpoint-specific optional result is neither
completion nor permission to continue after an unknown or failed acknowledgement.
Only the documented bulk protocol is supported; single-asset mode refuses.
Unknown session, bucket and deployment acknowledgements emit one finite JSON
diagnostic on stderr, separate from result and public operation contracts.
Allowlisted phase/category and a bounded integer HTTP status identify the stopped
attempt without provider text, private JWTs, credentials or bodies. Protocol
validation remains distinct from transport/timeout/HTTP failure. These observations
grant no retry, continuation, repair or inference that a write was not accepted.
Upload completion is not Preview deployment, content/readback/public proof, live
acceptance, capacity safety or permission to activate a registration. These remain
separate workflow/adaptor work; no actual provider write is exercised by mocks.

`lib/process/cfworker/create.py` binds one atomic name-only Preview collection
POST to the fresh exact deploy reservation, verified immutable handoff and supplied
complete selected-absence status. The fixed `ignore_base_config=true` query prevents
implicit Base inheritance. Catalogs of 99 or more Previews refuse, leaving headroom
below the documented minimum 100-Preview quota; no eviction or cleanup endpoint
exists. Content and complete status are rechecked before the single attempt is
consumed. A lock serializes one instance, not durable or cross-instance execution.
Response loss, malformed identity and transport/cleanup failure remain unacknowledged;
no retry, fallback or unknown recovery write exists. Finite acknowledgements omit
credentials and arbitrary provider fields. Supplied readback confirms only the
created identity and unchanged parent/sibling identity catalog, not deployment,
content, public reachability or an applied operation. Prepared observations cannot
reserve quota atomically; trusted workflow composition must authenticate current
admission, registration and reservation and establish resource exclusivity and
serialization. Acknowledgement is not original-writer quiescence. Deployment writes
remain separate until complete deployment-history preconditions can avoid native
automatic old-deployment eviction. No live call, registration activation, workflow,
release or installed CLI is exercised by this source primitive or its mock tests.

`Reader.history` binds the complete currently retained deployment catalog to a
present selected Preview, with fixed page/per_page GETs and bounded pagination.
Repeated catalogs and complete status observations must agree, including parent
and sibling identities. Missing metadata, duplicate/foreign UUIDs, failed reads
and movement refuse, not prove absence. Identity-only control-plane status permits
undeployed Previews with absent/empty optional URLs; any nonempty URL still must
match prepared routing. Strict deployed Preview-address observation is unchanged.
The catalog is not an all-time history, content proof or atomic provider snapshot.

`apply.deployment_prepare` binds a finite write plan to the exact fresh apply
reservation, prepared parent policy and supplied complete selected-present catalog.
Absent settled material needs no retained deployment; present material needs some.
Ambiguous Preview ID/name routing and catalogs of 99 or more deployments refuse.
`writer.Deployer` holds separate writer and private completion custody, serializes
one instance and sends one assets-only verified multipart POST to that immutable
Preview ID with fixed `ignore_defaults=true`. Each payload revalidates the exact
handoff and complete supplied pre-write observation. An attempt is spent before
I/O; no retry, latest, patch, fallback, cleanup or unknown recovery write exists.
Finite acknowledgements omit private fields and credentials. Supplied readback
requires unchanged parent/Preview/sibling identities and every previous deployment
plus the exact new deployment, not an applied outcome, content/public proof or
original-writer quiescence. Quota headroom is not atomic capacity reservation;
trusted workflow must establish exclusive serialized custody, authenticated fresh
admission, registration/reservation and actual content/material observations.
No live call, activation, workflow, release or installed CLI is proved by mocks.

`reader.Readback` holds no credentials and derives its fixed immutable workers.dev
origin from the exact confirmed deployment UUID and prepared parent suffix, never
provider-returned URLs, mutable lane addresses or latest selectors. DNS limits
refuse without truncation. Fresh apply/handoff validation precedes anonymous GETs
of every vetted asset, including the identity file, through fixed canonical HTML
routes. Ambiguous or unsupported canonical routing refuses before I/O. Exact
bounded bytes, fixed media types and vetted security/indexing headers must agree;
redirects, transformation, partial/error replies and transport/cleanup failures
refuse without retries, fallback or mutations. Handoff bytes are revalidated after
all reads. The finite result binds request, artifact, deployment and immutable
address; it proves neither current mutable-lane selection, atomic provider state,
original-writer quiescence, authenticated admission nor public evidence publication.
The 30-second transport limit is a network idle timeout, not a whole-read deadline.
Unknown reservation observation is admitted separately from fresh write payloads;
no state settlement or workflow is supplied by the reader.

`apply.resume` admits read-only observation of the exact original unknown request,
with unchanged registration, revision, active request/assessment and historical
settled material. `candidate` requires retained old deployments and exactly one
new selected UUID with unchanged parent/Preview/siblings, never latest inference.
Neither action grants a new writer attempt. All upload/create/deployment payloads
still require a fresh reserved plan; unknown plans refuse those writes.
`apply.settle` validates supplied finite content readback against the exact request,
artifact, confirmed deployment, prepared immutable address and bounded file count,
and requires explicit idle original writer. It returns the existing shared applied
operation. `unknown` preserves unavailable/unverified reasons in that same contract;
the existing state kernel retains reservation/revision/historical material without
pretending an unverified new deployment is absent or the historical one is current.
`apply.published` uses independent prepared public readback to bind the exact
original request and assessment before returning an eligible applied operation.
Existing public publication/readback consumes shared records unchanged; no private
provider fields or second ledger enter the wire contract. These supplied projections
authenticate no observation, prove no actual quiescence and publish nothing alone.
Trusted workflow owns authenticated readback execution, fresh admission/registration,
exclusive writer custody and public-evidence-before-current-CAS ordering. No timeout
authorizes recovery or a second write, and no live/installed lifecycle is claimed.

Static qualification and output validation live in `lib/content/static/`.
Standalone and inline SVG accept only UTF-8 static elements and a narrow text-only `style`
element with optional exact `type="text/css"`. Its character data reuses the CSS
and local-reference checks, including media queries for adaptive icons, without
rewriting the vetted bytes. Child elements, other style attributes/types, foreign
namespace styles, entities and XML processing instructions refuse. Every admitted
local file reference must exist in the immutable content set. This is a bounded
static subset, not a complete CSS sanitizer or browser confinement proof. HTML
validation passes each complete original inline SVG fragment to the same validator;
unclosed or malformed fragments refuse and surrounding HTML remains checked.
`lib/media/node.py` builds only an exact clean checkout and registered static app,
using prepared tools outside product source and an allowlisted child environment.
Script-disabled installation alone may hold the package reader; full/head Guard
and build hold neither that reader nor provider/storage credentials. Independent
source acquisition is rechecked before any control invocation. Installation
policy overrides remain in that phase alone. Released Plumb projects its trusted
configuration into the acquired owned checkout without inherited source hooks or
configuration; tracked source and prepared tools must remain unchanged before
genuine full/head Guard and build proceed. Neither phase inherits installation
overrides or its reader configuration. Tool/source
changes refuse. These child boundaries are not a filesystem sandbox. Trusted
first-party lane build control may hold only the workload writer to vet, record
and independently fetch content. Install, Guard and build children receive no
storage/provider writer; the job holds no deployment, lane-state or public-evidence
write authority.
`lib/store/handoff.py` records vetted bytes, source manifest and fixed no-script,
no-external-content/no-store headers as an immutable verified content workload.
Its key binds exact source, Guard, tools and implementation, not environment names
or request IDs. Reuse is content evidence, never deployment authorization.

`lib/process/lane/entry.py` admits the exact shared dispatch against current trusted
registration/state and independently observed fixed-main control run, both actors'
push authority and remote source commit/tree. `build.py` binds its admitted source,
actual domain/tool/Guard world and vetted content to an immutable recorded workload.
`reservation.py` rereads admission, reserves only an exact apply/dispose request and
records create-only original run/attempt custody with independent readback. Inspect
reserves nothing. Busy state, CAS movement and ambiguous custody refuse; no retry,
takeover or unknown continuation writer is granted.

`execution.py` independently rereads original custody, registration/reservation and
current authenticated run authority before and after observation. `transfer.py`
holds exact bounded GET-only workload readers, independently validates every byte
and full artifact identity, and rechecks execution authority after consumption.
It runs no product checkout/build/script and accepts no writer or private fallback.
These observations are not bearer permits, provider results or writer quiescence.

`lane-intake.yml` exposes only a reusable workflow, with no dispatch/push/schedule
trigger. It remains admitted solely from the fixed-main `lane.yml` caller; it
cannot independently dispatch. Its outputs bind admission,
workload, reservation and verified consumption, not complete execution. The intake
concurrency group is distinct from the complete caller's custody group so nested
calls do not self-block. The complete entry connects admission and build to the
separate provider, immutable public evidence and final settlement stages.
No live or installed consumer is claimed by these source components.

`lib/process/lane/provider.py` binds fresh original-run execution to complete
cfworker observations under prepared parent/subdomain pins. Apply/dispose return
content-bound preflight observations, not bearer permits. Inspect consumes only
the saved verified workload for settled presence and checks every current-origin
asset; selected absence consumes no content and proves no URL teardown. Complete
status and execution authority are reread before a result is returned.

`disposal.py` permits only the fixed original dispose job and records a create-only,
fsynced attempt marker in its prepared runner temporary seat before one selected-ID
DELETE. The marker remains after failure and protects that seat against repeat
processes, not distributed or adversarial exactly-once execution. Retries with a
different original run/attempt refuse. Ambiguous acknowledgement returns unknown
without an idle claim, retry or cleanup. Applied absence additionally requires
independent complete selected absence, unchanged parent/siblings and fresh authority.

`deployment.py` consumes verified recorded content without product checkout or
execution. Fresh original run/attempt, registration/reservation and complete provider
state precede every creation, session, bucket and deployment write. Created identity
and siblings are independently confirmed; prior deployment history is retained.
Private completion JWT remains inside one controlled uploader/deployer process,
never in plans, job outputs or public records. Ambiguous writes stop all further
writes without retry, rollback or cleanup. The same local marker limitations apply.
Every vetted asset must match at both exact immutable UUID and prepared current lane
origins, followed by unchanged history and fresh authority. `Readback.current` is
idempotent; its result cannot replace immutable deployment proof in `apply.settle`.
Idle is supplied only after this synchronous acknowledged writer's attempts return;
it is not independent global quiescence or timeout-based recovery authority.

`lane-observe.yml`, `lane-deploy.yml` and `lane-dispose.yml` expose only callable
reusable units. They inherit the authenticated fixed-main caller context and accept
only explicit prepared readers and the selected provider writer, never bucket
writers or inherited secrets. Runtime revalidates A's exact authenticated boundary;
The complete entry connects all producer dependencies and serializes custody.
These units publish no public evidence and settle no current state; the subsequent
evidence/state composition owns those obligations. Tests prove neither live
topology, physical exclusivity, provider credential scope nor installed behavior.

`lib/process/lane/publication.py` consumes B's finite original-run result only
after fresh authenticated execution, exact request/assessment and declaration
binding. Supplied result digests and plans are observations, never bearer permits;
private plans are not published. Prepared publication authority and bare origin
remain outside inputs. Its writer holds only the independently prepared evidence
bucket and exposes create/get for one exact immutable key/body, with fresh original
authority and registration/reservation before and after store access. Private
responses share the 64 KiB evidence document budget and refuse transformation;
the fixed transport accepts only GET and conditional create-only PUT, with a
30-second network idle timeout, not a whole-operation deadline. Only 412 and the
observed 409 with a well-formed S3 Error containing exactly one leaf Code equal to
ObjectLockedByBucketPolicy enter collision readback; unrelated or malformed 409,
403 and ambiguous responses refuse. This evidence-only mapping does not change
generic R2 state or release writes and does not itself establish identical bytes.
The bounded retained onboarding probe qualifies transport, not a live lane run.
Identical
collisions still require independent anonymous canonical byte/digest readback;
different bytes, unknown writes and failed public reads refuse without retry,
private fallback, rollback or current-state update. A second public read binds the
entire exact operation before a final authority check returns only its immutable
shared reference. Applied/refused/unknown remain distinct; publication does not
turn non-applied observations into completion or new provider-write authority.

`lane-evidence.yml` is a non-dispatching reusable unit with separate evidence and
state jobs. The evidence job holds its evidence-only writer and required
state/control/product readers, never provider/workload/state writers, source
acquisition, product execution or inherited secrets. Complete caller wiring
names every producing stage; publication alone cannot authenticate prior writes,
prove global writer quiescence, physical credential scope, origin/bucket mapping,
retention or live topology. Trusted onboarding and the complete workflow own those
facts. The separate state job performs verified-public-evidence-before-current-CAS settlement.
No live or installed capability is claimed by these source tests.

`lib/process/lane/settlement.py` consumes only the original request/prepared value
and C's public reference. It reauthenticates the original run/attempt, actors,
registration, reserved current and custody, then independently GETs the exact
attempt's native jobs with Actions read permission. A complete single default
page of at most 30 unique job names/IDs is required; truncation, another run/head,
unexpected jobs, producer failure/cancellation or nonterminal writers refuse.
`execution.producer_names` fixes the complete caller's reusable job display names
and action-specific success/skipped conclusions supplied by the complete graph,
including both conditional provider writers. An inactive provider call can
appear as a skipped caller-only name instead of its expanded reusable name;
both presentations identify the same branch and duplicates still refuse.
Only the current state job may
remain in progress. These authenticated native conclusions prove producer
quiescence, not provider effect or physical credential custody.

The independent anonymous applied record must bind the entire original request
and assessment. Public unknown/refused records, unavailable output and failed
readback leave the original reservation, revision, observed material and history
unchanged; no synthetic outcome is recorded. Inspect verifies evidence without
state writes. Mutation reuses `state.record` through an exact-key/body wrapper:
only immutable verified operation history and its one expected current CAS can
be written. Fresh authority, native quiescence and public readback precede each
write. Conflicts and ambiguous writes refuse without retry or rollback; history
may already exist when the current CAS refuses. Success additionally requires
independent exact current, registration and original custody readback. Disposal
advances revision with an absent tombstone, never removes current or history.
Registration/current remain separate objects, not an atomic authorization
transaction. A later settled/unknown/replayed execution cannot continue writing.

The state job in `lane-evidence.yml` holds only the state writer and required
readers, and admits no provider, evidence or workload writer. It supplies no
recovery, takeover, second provider attempt or timeout-based repair. Source
regressions prove neither the complete live caller nor installed consumption.

`lane.yml` is the sole fixed-main exact-request dispatch entry. One non-cancelling
global custody group serializes static lanes without overlapping nested intake
custody. Intake, complete provider observation and the selected apply/dispose call
are explicit direct producers of evidence; failed, cancelled or unexpectedly
skipped stages cannot enter publication. Inspect calls neither provider writer.
Evidence and state remain distinct native jobs in one callable composition so
the final state job can independently observe every completed producer, including
its evidence job, without depending on a nonterminal separate reusable caller.
Intake content consumption succeeds for all actions; non-apply consumes no new
workload. Unknown/refused provider observations may produce public evidence but
never settle current. Missing results, failed publication or cancelled writers
leave reservations intact; no synthetic failure, retry or automatic recovery is
introduced. Explicit job environments retain reader/writer separation; provider
and bucket grants, public origin, retention and parent pins still require trusted
operational onboarding. Formal release workflows are unreachable from this entry.
Source regressions do not prove registration activation, real Crest operations,
browser access, timings or installed Plumb consumption; those remain downstream.

`lib/content/static/runtime.py` owns the bounded local Preview guest primitive.
Only disposable temporary product directories and separate read-only trusted
controls are mounted. The public Images stable reference and resource budgets
live in `resources/build.json`; the fixed Docker profile permits ordinary bridge
networking, never host networking. No host credentials, home or Docker socket enter
the guest. Network reachability is not credential authority or a confinement proof.
Stdout alone is returned; stderr remains diagnostic. Both streams are
drained live without a TTY under one aggregate output budget, deadline and
workflow-command suppression. Stream separation is not proof authentication;
product-authored stdout remains untrusted. Every exit path reconciles only the
invocation-labelled container, removes it and independently confirms absence;
failed or unknown teardown admits no output. Client timeout is not cancellation.
Stable is prepared first; the guest runs only its resolved digest, recorded in
host-observed identity, never a guest-authored Guard proof.
This primitive grants no source/target authority, package reader or publication.
`lib/content/static/bridge.py` binds this runtime to static content through
separate online frozen install, full head Guard and build guests. The explicit
install disables product scripts and pnpm hooks without requiring an offline cache.
Every guest excludes readers and deployment/storage writers. Each must disappear
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
Origin readback admits only the exact independently selected repository's
credential-free GitHub HTTPS address, with or without terminal `.git`, as the
official checkout writes the suffix-free form. No other spelling is normalized.
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
The static bridge does not invoke this collector yet. Reader authorization,
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
full/head Guard there unless an earlier marker of the same repository and commit
or the organization Guard of the pull request that produced the marker's tree
already verified exactly the same combination. Its record keeps the original
marker identity, effective Git tree, exact package set, control authorities and
the genuine Guard result: one Guard actually produced for this exact combination,
in this run, that earlier one or that pull request. A verified snapshot is filed create-only under
`verified/<owner>/<repository>/<commit>/<digest>.json`, the digest covering every
combination field except the marker, and the first writer wins, since two genuine
runs of one combination record different execution worlds. Reuse still prepares
current stable controls and lift, rechecks the filed Guard against the running
authority and this resolution, and names the marker and run it came from in the
plan. Any difference runs Guard again, and nothing filed under another commit is
recalled.
When nothing is recalled, the plan next takes the organization Guard's verdict
from the pull request that produced the marker's tree before it runs Guard
itself. The organization Guard uploads its runtime document as the
`guard-runtime` artifact; `lib/identity/guard/pull.py` lists those artifacts,
keeps the ones whose run head has the marker's tree, and downloads each through
its redirect without the token. Such a document proves a different commit and
the checked-out tree instead of the lifted one, so the proof check binds it to
the marker's tree, to this resolution's `ci-latest` tree and packages, and to
the running authority, never to a commit or to the root it ran in, which the
organization workflow reports as `.`; the plan's own Guard keeps the stricter
binding to its head and its absolute root. The first admitted verdict stands as the
snapshot's Guard and is filed create-only like one the plan produced. The plan
names the run, artifact and head it came from, or why none was taken, under
`pulled` in its context. An unreadable listing, no matching artifact or a
rejected verdict runs Guard as before.
Builds recreate that exact combination and refuse movement. Linux snapshot
preparation installs the system OpenSSH server for declared SSH test prerequisites;
the isolated POSIX snapshot includes trusted system sbin directories after tool
path reconstruction. Preview guest tool discovery remains unchanged. A refused
control invocation names only its executable and exit status, never child output
or arguments. Compilation uses the
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
Prepared POSIX synchronous workers enforce an eight-second wall-clock budget from
application entry through body reading and dispatch acknowledgement, with a
five-second upstream socket timeout. Existing process timers and threaded workers
refuse before dispatch. This bounds application handling, not transit or ingress
queueing before application entry. Unknown acknowledgement returns 503 and never
asserts that dispatch did not execute. Credential-free stderr records distinguish
unverified arrival identities from authenticated delivery/repository/head and
dispatching, dispatched or unverified outcomes; host log retention is required.
Recovery reads App delivery records, matching workflow runs and Auto/PR/Guard/merge
facts before one explicitly qualified original redelivery. A completed operation
needs no retry, and an unknown operation never authorizes an automatic retry.
GitHub.com exposes recent App deliveries and original redelivery for three days;
host logs and retained evidence must not imply indefinite provider replay.
See GitHub's [delivery retention and redelivery documentation](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks).
The receiver verifies the original webhook HMAC and signs a bounded push summary
with a separate purpose prefix. It holds no App private key. The workflow verifies
that signature, the configured dispatch sender, main runtime and minted installation
before qualifying current repository identity and its root Plumb declaration.

Configure `WHARF_FOLLOW_APP_CLIENT_ID` and `WHARF_FOLLOW_SENDER_ID` as trusted
Wharf variables. The workflow runs on the explicit GitHub-hosted `ubuntu-24.04`
class; it needs no runner labels, persistent data directory or recovery bucket.
`WHARF_FOLLOW_APP_PRIVATE_KEY` is the dedicated follow App secret and appears only
in this workflow. `WHARF_FOLLOW_WEBHOOK_SECRET` matches the receiver's webhook
secret; package resolution reads `PERISHLAB_PACKAGES_READ`. The App token is scoped
to the signed repository and exactly Contents, Pull requests and Issues write plus
Metadata, Checks and Actions read. The read grants verify the exact candidate's
required Guard check and workflow identity on private repositories. Registration,
installation approval and the workflow's explicit token request must agree. Read
back those grants, push subscription and installations before treating runtime
setup as complete.

Each run creates an owned disposable main checkout and private home with a fresh
Plumb state directory. They are removed after owned commands terminate, including
refusal and cancellation. No local plan, Git worktree or validation cache is
restored. Released `plumb follow` recovers from the registered GitHub Issue,
publication intent, branch, pull and exact Guard/merge facts, and obtains fresh
local proof before another delivery. Foreign, dirty or diverged source refuses.
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
| Objects | entries, binary, binaries, deb, suite, npm, oci, chart, cargo, cfworker, channel, distribution, plan, wharf, product, request, source, registry, installation, engines, machine, autocrlf, node, pnpm, plumb, manager, evidence, state, and the Depot kinds |

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
  The plan job also mints, for the same repository, actions and contents read
  alone as `WHARF_GUARD_TOKEN`, to read the pull request Guard verdict; Guard's
  environment never holds it.
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
