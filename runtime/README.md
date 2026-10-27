# Godot coordinator runtime (API 1)

`godot_coordinator.py` is the only implementation. The installer places a
content-addressed version outside skill/project contents and exposes
`~/.local/bin/godot-coordinator`. Callers resolve `GODOT_COORDINATOR` when set,
otherwise that stable path; a missing installation is an error, not permission
to use a private lock or start an uncoordinated engine. `--version` reports the
runtime and API versions.

## Admission

```sh
godot-coordinator run --mode shared --project /absolute/cache/project \
  --timeout 120 --purpose 'headless import' -- /path/to/Godot --headless --path /absolute/cache/project --editor --import
godot-coordinator run --mode capture --purpose 'capture batch' -- command-that-launches-coordinated-leaves
godot-coordinator run --mode exclusive --purpose 'timing measurement' -- measurement-command
godot-coordinator status --json
```

- A project is canonicalized through symlinks and identified by both path and
  filesystem device/inode. One engine/import owner may use that cache at a time.
  Different project directories may run in parallel. An overlay sharing the
  original project's import cache must claim the original root.
- A capture batch holds one of two machine-wide slots. Its individual engine
  leaves separately claim their projects. Outer controllers therefore omit
  `--project`. Authenticated nested captures reuse the ancestor batch slot.
- Exclusive work waits for all admitted work to drain. An earlier exclusive
  waiter prevents new independent shared/capture admissions. Nested work may
  finish an already-admitted reservation so draining does not deadlock.
  Project FIFO uses the same non-project prerequisites as actual admission:
  live inherited machine admission, exclusive barriers and capture capacity.
  A draining leaf may pass an older waiter only while those prerequisites
  block that waiter. Once eligible, the older waiter keeps project priority.
  Active project owners and physical foreign/legacy blockers are never bypassed.
- Project claims are **never inherited or reentrant**. A nested request against
  an ancestor's project is rejected with an actionable error; siblings against
  the same project serialize. Acquiring exclusive beneath shared/capture is
  rejected rather than waiting forever.
- `--timeout` is one total budget for admission, execution, and owned cleanup;
  cleanup never extends its deadline. Finite CLI runs reserve the final 0.3
  seconds for cleanup by ending work at total deadline minus 0.3 seconds.
  Admission still uses the total deadline, but admission too late for work
  starts no child, and the payload is never released after the work cutoff.
  A freshly proven completed, still-unreaped direct child may use the remaining
  total budget for safe completion resolution and preserve its actual exit
  status rather than become a work-cutoff timeout. A terminal leader alone is
  not sufficient: authenticated members, attached engines, pending launches,
  and retained unknown nested owners must be resolved before success.
  Timeout exits 124; cancellation uses 128 + signal; ordinary child exit codes
  propagate. Core timeouts alone emit stderr prefix `godot-coordinator: timeout: `;
  a child returning 124 does not emit that prefix.
  A reservation is retained if a registered indirect engine or unresolved
  pending launch still exists. Returning from a wrapper does not prove release.
- `--ready-fd N` writes one newline-delimited JSON object containing the actual
  direct child's PID and its token after durable registration. The helper execs
  the command without changing PID. Child stdout is untouched; coordinator
  diagnostics go to stderr. Status never includes tokens.
- Repeat `--child-env KEY=VALUE` for values that must reach only the actual
  command (for example macOS native guard injection). These values are sent to
  the final exec, not loaded into coordinator/helper Python interpreters.
  Coordinator credential/state/path keys cannot be overridden this way.

`check --mode shared|capture|exclusive` exits zero only when an active inherited
reservation covers that machine mode and the caller has genuine process
ancestry. A copied token alone is insufficient. `check --project ROOT` always
fails: it can never authorize a second engine to reuse a project claim.
A copied token cannot extend a reservation after the real job exits. Surviving
membership requires a previously established kernel birth/ancestry or a known
birth still anchoring the original session/group; token/PGID text alone never
adopts an unrelated process.

## Python contexts

Import the externally resolved runtime, not a vendored copy. A Python lease
provides admission, **not process supervision**. Prefer the canonical CLI
`run` examples above for actual engine and launcher commands, with project
ownership at the actual leaves.

Advanced Python embeddings must handle SIGTERM/SIGINT and the child-creation
race, forward cancellation only through owned child handles, and await child
and nested-family cleanup within the original absolute deadline. The context
does not interrupt its Python body; a raw `subprocess.run` inside a lease does
not by itself provide these lifecycle guarantees.

The yielded `.env` is a complete child environment, anchored to the absolute
state directory and coordinator path. Preserve its coordinator fields even
when changing child `HOME`/XDG settings. Local nesting uses context variables,
not process-wide `os.environ` changes. `lease(..., cancel=predicate)` polls the
zero-argument predicate while queued and removes its own waiter promptly.
After-yield cancellation remains the caller's responsibility.

Direct Python native callers must call `leaf.validate_command(command)` before
cache or project mutations and before launching the native command, under the
original lease. Keep that lease alive through setup, native execution,
persistence and cleanup. The method checks command domain admission only: it is
not proof of lease liveness, ownership, project-reuse permission, deadline
authority or signal authority. Continue to budget work with `.remaining()` and
retain the actual child handle for owned cleanup.

Public errors are `CoordinatorError` (exit code 2), `LeaseTimeout` (124), and
`CoordinatorCancelled` (130). Python bodies are cooperative: the context never
signals a PID discovered in a snapshot and does not interrupt arbitrary code.
`.deadline` is the earliest inherited monotonic deadline (or `None`);
`.remaining()` returns positive float seconds, `math.inf` if unbounded, and
raises `LeaseTimeout` if expired. Clamp it against a finite phase budget before
launching a child and when budgeting execution plus owned cleanup. The caller
retains its real, unreaped `Popen` handle for cleanup. An expired context raises
on exit and retains claims while its authenticated children remain alive.

The asset verifier's `run(...)` consumer uses a rootless Python context only to
carry a local capture batch's authenticated environment. A canonical CLI
`run --mode shared --project ROOT` owns each actual headless leaf; a launcher
instead receives a rootless shared CLI supervisor and claims its own native
leaves. Neither wrapper claims an ancestor project. The CLI supervisor stays in
the verifier's process group so an outer command owner can protect its cleanup.
Scoped nonraising SIGTERM/SIGINT handlers forward cancellation to the retained
direct supervisor and await canonical family cleanup within the original
absolute deadline; the verifier never SIGKILLs a launcher group first.
Timeout, signal cancellation and ordinary child statuses remain 124,
128 + signal and the actual child code. Child stdout and stderr are retained
separately (`*-stdout.log`, `*-stderr.log`), so admission diagnostics cannot
become the parsed version line; both streams still participate in error checks
and capture provenance retention.

## Indirect native launches

LaunchServices can reparent an engine, so a decimal PID file does not prove
ownership. The genuine leased monitor first registers an absent witness path
in an owned mode-0700 directory:

```sh
godot-coordinator register --pid-file /private/owned-dir/native.pid --binary /exact/native/binary
```

This creates the private mode-0600 `native.pid.launch.json` capability manifest.
It records API, capability, expected binary/project, absolute coordinator path
and state directory. The registry stores only the capability hash, and status
redacts credentials recursively.

The actual Python native wrapper retains its direct `Popen` child **unreaped**,
then runs the same external coordinator version from that manifest:

```sh
godot-coordinator witness --pid ACTUAL_PID --pid-file /private/owned-dir/native.pid \
  --launch-file /private/owned-dir/native.pid.launch.json
```

The coordinator verifies that the native's kernel parent is the witness CLI's
actual parent (the owning wrapper), plus exact executable, post-lease kernel
birth identity, and canonical project. It writes a strong JSON receipt containing
`api`, `pid`, `start`, `binary`, and `launchcapability`, and binds that exact
identity to the reservation. File observation can never learn ownership from a
replacement PID. A forged/stale receipt without central birth authorization
retains the unresolved claim; unknown fast-exit identity is not fabricated.
Explicit owned exported players need not have a Godot basename; unmanaged
discovery continues to use strict executable classification.

Numeric `register --pid PID --binary BINARY` is only for genuine current
descendants of the authenticated grant in **both** production and isolated
domains. It cannot adopt an unrelated same-UID process. `--binary` names the
actual kernel executable, not a launcher which starts another binary.

There is no indirect-PID termination API. Explicit cancellation persists the
wrapper's private cancellation marker. The owning wrapper checks it before and
after native birth and during execution, and terminates/reaps only its real
unreaped `Popen` child. Delayed LaunchServices starts therefore see cancellation
before creating a native. Hard-killing observers without the protocol retains
live/unresolved claims instead of inventing cleanup.

After actual wait/reap, the wrapper uses:

```sh
godot-coordinator complete --pid-file /private/owned-dir/native.pid \
  --launch-file /private/owned-dir/native.pid.launch.json --pid ACTUAL_PID
```

The capability permits this after monitor death, but completion still requires
the centrally established birth receipt and absence of the PID. Pre-birth
failure/cancellation may use `--not-launched` only while no receipt or native
identity exists. Monitor death never clears pending intent. Status exposes
pending paths/reasons and attached identities without secrets.


## State and rollout

Production has one fixed OS-per-UID namespace,
`/var/tmp/godot-coordinator-<uid>` (canonicalized on Darwin). Caller `HOME`, XDG
paths and `GODOT_COORDINATOR_DOMAIN` cannot select another production registry.
All installed versions use that same API-1 state, bound to the OS boot identity.
The state directory must be owned by the user and mode 0700; state/lock files
are owned regular single-link mode-0600 files. Final state symlinks are rejected.
Admission is one `flock`-protected atomic transaction across every resource,
with durable JSON replacement. Commands wait on a pipe until their kernel birth
identity is durable, closing the spawn/record crash gap. CLI cleanup signals
only its own `start_new_session` group while its direct leader remains
unreaped, preventing PID/group reuse. It never reaps and later signals that
group number.
Timeout/cancellation cleanup sends TERM to that owned unreaped group, then
promptly attempts the atomic nested-owner veto before safe escalation. There
is no guaranteed minimum TERM-handler grace for a timed-out unprotected leaf.
KILL requires authenticated ownership proof under the state lock; protected
nested supervisors and unresolved/unknown candidates veto group escalation,
and their claims remain retained. All possible group signals precede the final
wait/reap, including when safely resolving an already-completed direct child.
One exception-owned cleanup path performs group signaling; a timeout does not
start a second cleanup against a dying unreaped group. The unchanged total
deadline bounds cleanup attempts, not hard real-time return under unbounded
scheduler, filesystem, or `flock` stalls.
Lock acquisition and discovery poll cancellation/deadlines. Durable authenticated
cancel/release notices let a cancelled thread remove its barrier without waiting
for a contended lock; notices are deleted only after their state update commits.
Admission is rechecked after discovery and durable publication before any child
can begin.

Waiters already denied by refreshed internal machine prerequisites retain those
sufficient blockers without repeating foreign/legacy discovery. This is only a
denial optimization: every otherwise-eligible grant still performs fresh full
discovery and foreign-aware project/FIFO checks under the same lock. Status
always performs full discovery.

Private notices, capability manifests and native receipts are flushed into
hidden temporary files, then published by atomic no-replace rename
(`renamex_np(RENAME_EXCL)` on Darwin, `renameat2(RENAME_NOREPLACE)` on Linux).
Readers see either absence or a complete single-link file. Existing destinations
are never overwritten, and lack of a safe platform primitive fails closed.

An explicit nondefault `GODOT_COORDINATOR_STATE_DIR` is an isolated **synthetic
process-test domain**, not authorization for real Godot work. It bypasses host
engine discovery and permits actual Python fixtures instead of Godot binaries,
but never weakens ancestry, capability or birth-identity ownership proofs.
Direct Godot execution is rejected. Real-engine verification must use production
state. Yielded environments preserve the absolute original namespace.

Production discovery inspects actual executable paths, never the occurrence of
“Godot” in bridge/script arguments. Known unmanaged engines on separate roots
can coexist with shared work. Same-root engines block that project; unknown
engine ownership fails closed with PID/binary diagnostics; exclusive work waits
for all unmanaged engines. Headed unmanaged engines and recognized legacy Node
capture-matrix/parked-launcher producers count toward capture capacity, deduped
by ancestry. An unknown legacy capture producer blocks capture/exclusive but
not unrelated shared headless work. A long-lived preview HTTP server alone is
not treated as a permanent capture batch.

Unmanaged native project discovery ignores the user-argument bucket after the
first `--` or `++`. A single separated absolute `--path` is known only when its
canonical root matches the actual kernel-reported cwd and directly contains
`project.godot`. Relative `--path` is always unknown because post-parser
resolution cannot be proved; use an absolute root or the canonical authenticated
coordinator protocol. Repeated or inline selectors, missing or option-like
values, `--gdscript-docs`, and engine-bucket `-u`/`--upwards` remain unknown.
Leading-dash names ending in `project.godot` and any native-bucket project-file
positional/operand mixed with other native arguments are unknown because option
operands cannot safely be distinguished from project positionals. A sole native
absolute `project.godot` argument is known only when that file exists and its
canonical parent matches the actual kernel-reported cwd. Without a selector,
only a direct `project.godot` at that cwd identifies a root; discovery never
walks ancestors. Metadata existence alone cannot prove file loading or upward
fallback. These rules apply to unmanaged engines without trusting launcher
validation.

On Darwin, cwd discovery uses a fresh, uncached `PROC_PIDVNODEPATHINFO` lookup
through the existing libproc loader, not a helper process or an environment
variable. This private API is subject to change: the complete ctypes ABI layout
and exact returned byte count are guarded. Acceptance requires the expected
snapshot birth identity in fresh reads before and after lookup, a usable
absolute filesystem-decoded path without possible truncation, and directory
mode plus device/inode agreement between its vnode metadata and the named
path's stat (`st_dev` compared modulo 2^32). Zero device/inode values alone are
not failure. Unavailable or inconsistent metadata leaves attribution unknown;
there is no fabricated root, stale cache, or `lsof` fallback. No new dependency
or runtime compiler is needed.

Before the initial cutover, quiesce old capture producers whose active batch
phase cannot be established from process identity. No finite snapshot can
identify an arbitrary legacy producer's between-engine batch intent. The
coordinator never terminates that foreign work or invents ownership for it.
Already-existing legacy Chase timing leases still block production admission;
genuine wrapper ancestry, not `CHASE_ENGINE_LEASE_HELD`, is the only transitional
child exception. Legacy lease directories are never created, removed or forged
by this runtime.

This runtime does not change focus, parking, headless, mute, or engine-pin policy;
those remain enforced by the existing native launchers. Linux uses kernel start
ticks; Darwin uses libproc birth identity and exact executable paths. PID
snapshots establish observation/liveness, never authority to signal a nonchild.
Darwin argument/environment inspection queries the kernel's `KERN_ARGMAX`
before `KERN_PROCARGS2`; oversized buffers are rejected by XNU. Read failures
remain fail-closed and include their actual inspection error in foreign-owner
diagnostics rather than silently treating readable projects as unowned.
