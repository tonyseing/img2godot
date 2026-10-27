import ctypes
import errno
import importlib.util
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent
COORDINATOR = REPO / "runtime" / "godot_coordinator.py"
WAIT = 8.0

# Each admitted command connects to its own test-owned endpoint, then remains
# alive until explicitly released. A connection proves execution, not a wrapper
# announcement. The same process can launch genuine nested coordinator clients.
GATE_COMMAND = r"""
import json
import os
import socket
import subprocess
import sys

if sys.platform == "darwin":
    import ctypes
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidpath = libproc.proc_pidpath
    proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc_pidpath.restype = ctypes.c_int
    path = ctypes.create_string_buffer(4096)
    if proc_pidpath(os.getpid(), path, len(path)) <= 0:
        raise OSError(ctypes.get_errno(), "Could not read fixture kernel executable")
    binary = os.path.realpath(os.fsdecode(path.value))
    class BsdInfo(ctypes.Structure):
        _fields_ = [("ids", ctypes.c_uint32 * 12),
                    ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                    ("counts", ctypes.c_uint32 * 5), ("nice", ctypes.c_int32),
                    ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                            ctypes.c_void_p, ctypes.c_int]
    proc_pidinfo.restype = ctypes.c_int
    info = BsdInfo()
    if proc_pidinfo(os.getpid(), 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        raise OSError(ctypes.get_errno(), "Could not read fixture kernel birth identity")
    start = f"darwin:{info.start_sec}:{info.start_usec}"
elif sys.platform.startswith("linux"):
    binary = os.path.realpath(os.readlink("/proc/self/exe"))
    with open("/proc/self/stat") as process_stat:
        fields = process_stat.read().rsplit(") ", 1)[1].split()
    start = f"linux:{fields[19]}"
else:
    raise RuntimeError("Fixture executable identity requires Darwin or Linux")

host, port, nonce = sys.argv[1:4]
connection = socket.create_connection((host, int(port)), timeout=8)
connection.settimeout(None)
reader = connection.makefile("rb")

def reply(value):
    connection.sendall(json.dumps(value).encode() + b"\n")

reply({"nonce": nonce, "pid": os.getpid(), "binary": binary, "start": start,
       "token": os.environ.get("GODOT_COORDINATOR_TOKEN")})
children = {}
locks = {}
for line in reader:
    request = json.loads(line)
    if request["op"] == "release":
        break
    if request["op"] == "spawn":
        child = subprocess.Popen(request["argv"], stdin=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE if request.get("capture_stderr") else None,
                                 start_new_session=request.get("new_session", False))
        children[child.pid] = child
        reply({"pid": child.pid})
    elif request["op"] == "poll":
        child = children[request["pid"]]
        code = child.poll()
        stderr = (child.stderr.read().decode(errors="replace")
                  if code is not None and child.stderr is not None else "")
        reply({"returncode": code, "stderr": stderr})
    elif request["op"] == "ping":
        reply({"pid": os.getpid()})
    elif request["op"] == "exit":
        sys.exit(request["code"])
    elif request["op"] == "lock":
        import fcntl
        handle = open(request["path"], "r+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locks[request["path"]] = handle
        reply({"locked": True})
    elif request["op"] == "unlock-at":
        import select
        import time
        select.select([], [], [], max(0, request["at"] - time.monotonic()))
        locks.pop(request["path"]).close()
        reply({"unlocked": True})
for handle in locks.values():
    handle.close()
reader.close()
connection.close()
"""

CANCELLABLE_LEASE_COMMAND = r"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from godot_coordinator import CoordinatorCancelled, lease

cancellation = Path(sys.argv[2])
try:
    with lease(mode="exclusive", timeout=15, purpose="cancelled-python-lease",
               cancel=cancellation.exists) as admission:
        subprocess.run(json.loads(sys.argv[3]), env=admission.env, check=True)
except CoordinatorCancelled:
    sys.exit(42)
"""

BLOCKED_LEASE_COMMAND = r"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

runtime, cancellation_path, project, payload, host, port, nonce, timeout = sys.argv[1:]
sys.path.insert(0, runtime)
from godot_coordinator import CoordinatorCancelled, LeaseTimeout, lease

first_poll = True
cancellation = Path(cancellation_path)
def cancelled():
    global first_poll
    if first_poll:
        first_poll = False
        with socket.create_connection((host, int(port)), timeout=8) as event:
            event.sendall(json.dumps({"nonce": nonce, "pid": os.getpid()}).encode() + b"\n")
        # The parent only triggers cancellation after this event. Returning
        # False unconditionally forces a broken blocking flock to get stuck.
        return False
    return cancellation.exists()

try:
    with lease(mode="shared", project=project, timeout=float(timeout),
               purpose="blocked-transaction", cancel=cancelled) as admission:
        subprocess.run(json.loads(payload), env=admission.env, check=True)
except CoordinatorCancelled:
    sys.exit(42)
except LeaseTimeout:
    sys.exit(43)
"""


# These bootstraps change scheduling only. Every transaction, child creation,
# signal handler and main entry point still runs the original implementation.
WORK_CUTOFF_COMMAND = r"""
import contextlib
import json
import os
import select
import socket
import sys
import time

runtime, target, host, port, nonce, *argv = sys.argv[1:]
sys.path.insert(0, runtime)
import godot_coordinator as core

original_acquire = core._acquire
original_transaction = core._State.transaction
original_popen = core.subprocess.Popen
original_cleanup = core._cleanup_owned
ownership = None
helper = None
registered = False
gated = False
cleaning = False
connection = reader = None

def acquire(*args, **kwargs):
    global ownership
    ownership = original_acquire(*args, **kwargs)
    return ownership

def popen(*args, **kwargs):
    global helper
    process = original_popen(*args, **kwargs)
    command = args[0] if args else kwargs.get("args", [])
    if isinstance(command, list) and len(command) > 2 and command[2] == "_exec":
        helper = process
    return process

def cleanup(*args, **kwargs):
    global cleaning
    cleaning = True
    try:
        return original_cleanup(*args, **kwargs)
    finally:
        cleaning = False

@contextlib.contextmanager
def transaction(state, *args, **kwargs):
    global registered, gated, connection, reader
    work = ownership is not None and state is ownership._state and not cleaning
    phase = "heartbeat" if registered else "registration"
    if work and phase == target and not gated:
        gated = True
        connection = socket.create_connection((host, int(port)), timeout=8)
        reader = connection.makefile("rb")
        work_end = ownership.deadline - 0.3
        connection.settimeout(max(0.001, work_end - 0.05 - time.monotonic()))
        connection.sendall(json.dumps({
            "nonce": nonce, "pid": os.getpid(), "deadline": ownership.deadline,
            "helper_pid": helper.pid, "phase": phase}).encode() + b"\n")
        if json.loads(reader.readline()) != {"op": "resume"}:
            raise RuntimeError("Work transaction gate was not released")
        select.select([], [], [], max(0, work_end - 0.05 - time.monotonic()))
    with original_transaction(state, *args, **kwargs) as result:
        yield result
    if work:
        registered = True

core._acquire = acquire
core.subprocess.Popen = popen
core._cleanup_owned = cleanup
core._State.transaction = transaction
try:
    sys.exit(core.main(argv))
finally:
    if reader is not None:
        reader.close()
    if connection is not None:
        connection.close()
"""

CLEANUP_GATE_COMMAND = r"""
import contextlib
import json
import os
import signal
import socket
import sys
import time

runtime, host, port, nonce, *argv = sys.argv[1:]
sys.path.insert(0, runtime)
import godot_coordinator as core

original_transaction = core._State.transaction
original_cleanup = core._cleanup_owned
cleaning = False
gated = False
connection = reader = None

def cleanup(*args, **kwargs):
    global cleaning
    cleaning = True
    original_handler = signal.getsignal(signal.SIGTERM)
    def observed_term(signum, frame):
        original_handler(signum, frame)
        if connection is not None:
            connection.sendall(json.dumps({
                "event": "cancelled", "signal": signum}).encode() + b"\n")
    signal.signal(signal.SIGTERM, observed_term)
    try:
        return original_cleanup(*args, **kwargs)
    finally:
        signal.signal(signal.SIGTERM, original_handler)
        cleaning = False

@contextlib.contextmanager
def transaction(state, deadline=None, *args, **kwargs):
    global gated, connection, reader
    with original_transaction(state, deadline, *args, **kwargs) as result:
        if cleaning and not gated and kwargs.get("refresh") is False:
            gated = True
            connection = socket.create_connection((host, int(port)), timeout=0.1)
            reader = connection.makefile("rb")
            gate_end = min(deadline - 0.05, time.monotonic() + 0.2)
            connection.settimeout(max(0.001, gate_end - time.monotonic()))
            connection.sendall(json.dumps({
                "nonce": nonce, "pid": os.getpid(), "until": gate_end}).encode() + b"\n")
            if json.loads(reader.readline()) != {"op": "resume"}:
                raise RuntimeError("Atomic cleanup gate was not released")
            connection.sendall(b'{"event":"resumed"}\n')
        yield result

core._cleanup_owned = cleanup
core._State.transaction = transaction
try:
    sys.exit(core.main(argv))
finally:
    if reader is not None:
        reader.close()
    if connection is not None:
        connection.close()
"""


REAPED_NESTED_OWNER_COMMAND = r"""
import contextlib
import errno
import json
import os
import socket
import sys
import time

runtime, mode, target_purpose, host, port, nonce, *argv = sys.argv[1:]
sys.path.insert(0, runtime)
import godot_coordinator as core

original_capture = core._capture_nested_owners
original_transaction = core._State.transaction
original_cleanup = core._cleanup_owned
original_getpgid = os.getpgid
owner = ownership = None
capture_pending = continuation_pending = False
captured = continuing = cleaning = released = inject_unknown = False
connection = reader = None

def barrier(event):
    global connection, reader, released, inject_unknown
    gate_end = min(ownership.deadline - 0.3, time.monotonic() + 8)
    if connection is None:
        connection = socket.create_connection(
            (host, int(port)), timeout=max(0.001, gate_end - time.monotonic()))
        reader = connection.makefile("rb")
    connection.settimeout(max(0.001, gate_end - time.monotonic()))
    connection.sendall(json.dumps({
        "nonce": nonce, "pid": os.getpid(), "event": event,
        "owner": owner, "until": gate_end}).encode() + b"\n")
    request = json.loads(reader.readline())
    if request == {"op": "release"}:
        # Test teardown releases either barrier and disables any later one.
        released = True
        inject_unknown = False
    elif request == {"op": "resume"}:
        inject_unknown = event == "captured" and mode != "esrch"
    else:
        raise RuntimeError("Retained-owner barrier was not released")
    connection.sendall(b'{"event":"resumed"}\n')

def capture(data, admission, candidates):
    global owner, ownership, captured, capture_pending
    original_capture(data, admission, candidates)
    if captured or cleaning or released:
        return
    for record in data["records"]:
        candidate = record["owner"]
        key = (candidate["pid"], candidate["start"])
        if record["purpose"] == target_purpose and key in candidates:
            owner = dict(candidates[key])
            ownership = admission
            captured = capture_pending = True
            break

def getpgid(pid):
    global continuation_pending
    try:
        result = original_getpgid(pid)
    except OSError as error:
        if (not inject_unknown or owner is None or pid != owner["pid"]
                or error.errno != errno.ESRCH):
            raise
        # The real target lookup has established the fixture precondition:
        # this particular saved nested PID is actually no longer observable.
        # Inject only the native API response, never a private veto/result.
        continuation_pending = True
        if mode == "eperm":
            raise PermissionError(errno.EPERM, "Controlled native lookup denial") from None
        return original_getpgid(os.getpid())  # Genuine different-session PGID.
    if inject_unknown and owner is not None and pid == owner["pid"]:
        raise RuntimeError("Reaped fixture PID was unexpectedly reused")
    return result

def cleanup(*args, **kwargs):
    global cleaning
    cleaning = True
    try:
        return original_cleanup(*args, **kwargs)
    finally:
        cleaning = False

@contextlib.contextmanager
def transaction(state, *args, **kwargs):
    global capture_pending, continuation_pending, continuing
    if continuation_pending and not continuing and not cleaning and not released:
        continuation_pending = False
        continuing = True
        # Reaching the next work iteration proves the previous original
        # heartbeat did not complete the batch on an unknown native response.
        barrier("unknown-continuation")
    with original_transaction(state, *args, **kwargs) as result:
        yield result
    if capture_pending and not cleaning and not released:
        capture_pending = False
        # AFTER the original context exits: committed state and no flock held.
        # The nested actual parent can now finish and reap its real child.
        barrier("captured")

core._capture_nested_owners = capture
core._State.transaction = transaction
core._cleanup_owned = cleanup
os.getpgid = getpgid
try:
    sys.exit(core.main(argv))
finally:
    if reader is not None:
        reader.close()
    if connection is not None:
        connection.close()
"""


PROCESS_METADATA_COMMAND = r"""
import json
import sys
import time

sys.path.insert(0, sys.argv[1])
from godot_coordinator import _Processes, _engine_project

pid = int(sys.argv[2])
processes = _Processes(deadline=time.monotonic() + 5)
arguments = processes.arguments(pid)
environment = processes.environment(pid)
print(json.dumps({"arguments": arguments, "project": _engine_project(processes, pid),
                  "token": (environment or {}).get("GODOT_COORDINATOR_TOKEN"),
                  "inspection_error": processes.inspection_errors.get(pid)}))
"""


ATOMIC_PUBLICATION_COMMAND = r"""
import json
import os
import socket
import sys
from pathlib import Path

runtime, target, host, port, nonce, value = sys.argv[1:]
sys.path.insert(0, runtime)
from godot_coordinator import _write_private_json

target = Path(target)
connection = socket.create_connection((host, int(port)), timeout=8)
reader = connection.makefile("rb")
def reply(value):
    connection.sendall(json.dumps(value).encode() + b"\n")

reply({"nonce": nonce, "pid": os.getpid()})
original_open = os.open
paused = False
def controlled_open(path, flags, *args, **kwargs):
    global paused
    fd = original_open(path, flags, *args, **kwargs)
    if (not paused and flags & os.O_CREAT and flags & (os.O_WRONLY | os.O_RDWR)
            and Path(os.fsdecode(path)).resolve().parent == target.parent.resolve()):
        paused = True
        # This is an actual created file and fd, before any payload write.
        # Instrument scheduling only; do not fake or replace filesystem I/O.
        reply({"event": "created-before-body"})
        if json.loads(reader.readline())["op"] != "resume":
            raise RuntimeError("Publication barrier was not released")
    return fd

os.open = controlled_open
if json.loads(reader.readline())["op"] != "publish":
    raise RuntimeError("Publication was not requested")
_write_private_json(target, json.loads(value))
reply({"event": "published"})
for line in reader:
    request = json.loads(line)
    if request["op"] == "release":
        break
    if request["op"] == "retry":
        try:
            _write_private_json(target, request["value"])
        except FileExistsError:
            reply({"refused": True})
        else:
            reply({"refused": False})
reader.close()
connection.close()
"""

PRIVATE_JSON_READER_COMMAND = r"""
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from godot_coordinator import _read_private_json
print(json.dumps(_read_private_json(Path(sys.argv[2]))))
"""


class Gate:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.nonce = os.urandom(16).hex()
        self.connection = None
        self.reader = None
        self.identity = None
        self.released = False

    def command(self, ignore_term=False, notify_term=False, notify_term_at=False):
        host, port = self.listener.getsockname()
        source = GATE_COMMAND
        if notify_term or notify_term_at:
            event = ("{'event': 'term', 'at': time.monotonic()}" if notify_term_at
                     else "{'event': 'term'}")
            source = ("import signal\nimport time\n"
                      "def report_term(signum, frame):\n"
                      f"    reply({event})\n"
                      "signal.signal(signal.SIGTERM, report_term)\n" + source)
        elif ignore_term:
            source = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + source
        return [sys.executable, "-u", "-c", source, host, str(port), self.nonce]

    def ready(self):
        return bool(select.select([self.listener], [], [], 0)[0])

    def accept(self):
        self.connection, _ = self.listener.accept()
        self.connection.settimeout(WAIT)
        self.reader = self.connection.makefile("rb")
        self.identity = json.loads(self.reader.readline())
        if self.identity["nonce"] != self.nonce:
            raise AssertionError("A non-owned process connected to the test gate")
        return self.identity

    def request(self, message):
        self.connection.sendall(json.dumps(message).encode() + b"\n")
        return json.loads(self.reader.readline())

    def spawn(self, command, new_session=False, capture_stderr=False):
        request = {"op": "spawn", "argv": command, "new_session": new_session,
                   "capture_stderr": capture_stderr}
        return NestedProcess(self, self.request(request)["pid"])

    def release(self):
        if self.connection is not None and not self.released:
            self.released = True
            try:
                self.connection.sendall(b'{"op":"release"}\n')
            except OSError:
                pass

    def expect_closed(self, timeout=0.75):
        self.connection.settimeout(timeout)
        try:
            value = self.reader.read(1)
        except (TimeoutError, OSError) as error:
            raise AssertionError("Owned command remained alive after cleanup") from error
        if value != b"":
            raise AssertionError("Expected owned command connection to close")

    def close(self):
        self.release()
        if self.reader is not None:
            self.reader.close()
        if self.connection is not None:
            self.connection.close()
        self.listener.close()


class NestedProcess:
    def __init__(self, driver, pid):
        self.driver = driver
        self.pid = pid
        self.returncode = None
        self.stderr = ""

    def poll(self):
        if self.returncode is None:
            result = self.driver.request({"op": "poll", "pid": self.pid})
            self.returncode = result["returncode"]
            self.stderr = result["stderr"]
        return self.returncode

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("nested coordinator", timeout)
            time.sleep(0.01)
        return self.returncode


class LeaseCommandValidationTests(unittest.TestCase):
    """Future public API contract; absent API is not the consumer behavior RED."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="godot-command-validation-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        spec = importlib.util.spec_from_file_location("command_validation_coordinator", COORDINATOR)
        self.coordinator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.coordinator)

    def admission(self, domain="isolated", deadline=None):
        # Pure command-domain validation must not consult a registry, inspect
        # processes, or imply project ownership/liveness/signal authority.
        return self.coordinator.Lease(
            {"GODOT_COORDINATOR_STATE_DIR": str(self.tmp / "unused-state")},
            "command-validation-fixture", SimpleNamespace(domain=domain), deadline, None)

    def fixture(self, name):
        executable = self.tmp / name
        executable.write_text(f"#!{sys.executable}\nraise AssertionError('must never execute')\n")
        executable.chmod(0o700)
        return executable

    def test_isolated_named_engines_raise_existing_domain_error_without_execution(self):
        admission = self.admission()
        for name in ("Godot", "godot4", "godot3", "Godot_v4.5", "godot.mono", "godot-headless"):
            with self.subTest(name=name):
                executable = self.fixture(name)
                with self.assertRaises(self.coordinator.CoordinatorError) as rejected:
                    admission.validate_command([str(executable), "--version"])
                self.assertEqual(rejected.exception.exit_code, 2)
                self.assertEqual(str(rejected.exception),
                                 "Explicit nondefault state is an isolated synthetic-test domain, "
                                 "not authorized for real Godot engines; use the production default state")
        self.assertFalse((self.tmp / "unused-state").exists())

    def test_isolated_validation_uses_resolved_target_not_symlink_name(self):
        neutral = self.fixture("neutral-fixture")
        engine = self.fixture("Godot")
        engine_alias = self.tmp / "Godot-alias"
        engine_alias.symlink_to(neutral)
        neutral_alias = self.tmp / "neutral-alias"
        neutral_alias.symlink_to(engine)
        admission = self.admission()
        before = dict(admission.env)
        self.assertIsNone(admission.validate_command([str(engine_alias), "--version"]))
        with self.assertRaisesRegex(self.coordinator.CoordinatorError, "isolated synthetic-test domain"):
            admission.validate_command([str(neutral_alias), "--version"])
        self.assertEqual(admission.env, before)
        self.assertFalse((self.tmp / "unused-state").exists())

    def test_production_domain_validation_does_not_reject_engine_class(self):
        # Metadata-only domain fixture: never open or lease production state.
        admission = self.admission(domain="production")
        self.assertIsNone(admission.validate_command([str(self.fixture("Godot")), "--version"]))
        self.assertFalse((self.tmp / "unused-state").exists())

    def test_command_domain_validation_does_not_replace_deadline_check(self):
        admission = self.admission(deadline=time.monotonic() - 1)
        self.assertIsNone(admission.validate_command([str(self.fixture("neutral-fixture"))]))
        with self.assertRaises(self.coordinator.LeaseTimeout):
            admission.remaining()
        self.assertFalse((self.tmp / "unused-state").exists())


    def test_isolated_near_miss_names_do_not_expand_engine_classification(self):
        admission = self.admission()
        for name in ("neutral-fixture", "godotx", "my-godot"):
            with self.subTest(name=name):
                self.assertIsNone(admission.validate_command([str(self.fixture(name)), "--version"]))
        self.assertFalse((self.tmp / "unused-state").exists())


class NativeProjectDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="godot-discovery-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        spec = importlib.util.spec_from_file_location("discovery_coordinator", COORDINATOR)
        self.coordinator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.coordinator)

    def project(self, name):
        root = self.tmp / name
        root.mkdir()
        (root / "project.godot").write_text("[application]\n")
        return root.resolve()

    def foreign_engine(self, arguments, cwd):
        # This is metadata only: no native engine or host process scan is needed.
        class Metadata:
            deadline = None
            cancel = None
            inspection_errors = {}
            items = {4242: {"pid": 4242, "ppid": 1, "start": "fixture:1"}}

            def executable(self, pid):
                return "/fixture/Godot"

            def arguments(self, pid):
                return ["/fixture/Godot", *arguments]

            def cwd(self, pid):
                return cwd

            def descendant(self, pid, roots):
                return False

        foreign = self.coordinator._foreign({"records": []}, Metadata(), "production")
        self.assertEqual(len(foreign), 1)
        self.assertEqual(foreign[0]["pid"], 4242)
        self.assertEqual(foreign[0]["kind"], "engine")
        return foreign

    def assert_blocking(self, foreign, project, expected):
        record = {"id": "request", "machine_root": "request", "mode": "shared",
                  "capture_root": None, "project": str(project), "sequence": 1}
        blockers = self.coordinator._blockers({"records": []}, record, foreign, [])
        self.assertEqual(bool(blockers), expected, blockers)
        if expected:
            self.assertEqual([item["pid"] for item in blockers], [4242])

    def cwd_child(self, directory):
        gate = Gate()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GODOT_COORDINATOR_")}
        environment["PWD"] = str(directory)
        command = [argument for argument in gate.command() if argument != "-u"]
        process = subprocess.Popen(
            command, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

        def cleanup():
            gate.release()
            try:
                process.communicate(timeout=WAIT)
            except subprocess.TimeoutExpired:
                process.kill()  # Only this fixture's actual unreaped direct child.
                process.communicate(timeout=WAIT)
            finally:
                gate.close()

        self.addCleanup(cleanup)
        self.assertTrue(select.select([gate.listener], [], [], WAIT)[0],
                        "Real cwd fixture did not connect")
        self.assertEqual(gate.accept()["pid"], process.pid)
        return process, gate

    @unittest.skipUnless(sys.platform == "darwin", "Requires real Darwin cwd metadata")
    def test_darwin_fresh_cwd_attributes_real_unicode_symlink_and_renamed_directory(self):
        original = self.project("arbol 雪")
        alias = self.tmp / "enlace 雪"
        alias.symlink_to(original, target_is_directory=True)
        child, gate = self.cwd_child(alias)
        processes = self.coordinator._Processes(deadline=time.monotonic() + WAIT)
        self.assertEqual(self.coordinator._engine_project(processes, child.pid), str(original))
        known = self.foreign_engine([], processes.cwd(child.pid))
        self.assertEqual(known[0]["project"], str(original))
        self.assert_blocking(known, original, True)

        renamed = self.tmp / "renombrado 雪"
        original.rename(renamed)
        renamed = renamed.resolve()
        decoy = self.project(original.name)
        self.assertEqual(alias.resolve(), decoy)
        # The same snapshot must read the fresh vnode, not PWD, argv, or a
        # cached pathname now naming the replacement project.
        self.assertEqual(self.coordinator._engine_project(processes, child.pid), str(renamed))
        moved = self.foreign_engine([], processes.cwd(child.pid))
        self.assertEqual(moved[0]["project"], str(renamed))
        self.assert_blocking(moved, renamed, True)
        self.assert_blocking(moved, decoy, False)
        self.assertEqual(gate.request({"op": "ping"})["pid"], child.pid)

    @unittest.skipUnless(sys.platform == "darwin", "Requires real Darwin cwd metadata")
    def test_darwin_untrusted_cwd_metadata_cannot_narrow_foreign_project_blocking(self):
        actual = self.project("actual 雪")
        decoy = self.project("decoy 雪")
        child, gate = self.cwd_child(actual)
        coordinator = self.coordinator
        processes = coordinator._Processes(deadline=time.monotonic() + WAIT)
        self.assertEqual(processes.cwd(child.pid), actual)
        library = coordinator._darwin_lib()
        rows = ("short", "api-error", "birth-before", "birth-after", "empty",
                "unterminated", "saturated", "relative", "different-path",
                "different-device", "different-inode")
        for row in rows:
            with self.subTest(observation=row):
                class UntrustedCwd:
                    saw_cwd = False

                    def __getattr__(self, name):
                        return getattr(library, name)

                    def proc_pidinfo(self, pid, flavor, argument, output, capacity):
                        used = library.proc_pidinfo(pid, flavor, argument, output, capacity)
                        if pid != child.pid or flavor not in (3, 9):
                            return used
                        if used != capacity:
                            raise AssertionError("Real kernel cwd fixture became unavailable")
                        if flavor == 3:
                            if ((row == "birth-before" and not self.saw_cwd)
                                    or (row == "birth-after" and self.saw_cwd)):
                                birth = ctypes.cast(
                                    output, ctypes.POINTER(coordinator._BSDInfo)).contents
                                birth.start_usec += 1
                            return used
                        self.saw_cwd = True
                        info = ctypes.cast(
                            output, ctypes.POINTER(coordinator._ProcVnodePathInfo)).contents
                        if row == "short":
                            return used - 1
                        if row == "api-error":
                            return 0
                        if row == "empty":
                            info.pvi_rdir = info.pvi_cdir
                            info.pvi_cdir.vip_path = b""
                        elif row in ("unterminated", "saturated"):
                            raw = b"/" + b"x" * 1022 + (
                                b"\0" if row == "saturated" else b"x")
                            address = (ctypes.addressof(info.pvi_cdir)
                                       + coordinator._VnodeInfoPath.vip_path.offset)
                            ctypes.memmove(address, raw, len(raw))
                        elif row == "relative":
                            info.pvi_cdir.vip_path = os.fsencode(actual.name)
                        elif row == "different-path":
                            # A plausible existing direct project, but not the
                            # vnode represented by the real returned metadata.
                            info.pvi_cdir.vip_path = os.fsencode(decoy)
                        elif row == "different-device":
                            info.pvi_cdir.vip_vi.vi_stat.vst_dev ^= 1
                        elif row == "different-inode":
                            info.pvi_cdir.vip_vi.vi_stat.vst_ino ^= 1
                        return used

                with patch.object(coordinator, "_darwin_lib", return_value=UntrustedCwd()):
                    observed = processes.cwd(child.pid)
                    self.assertIsNone(observed, row)
                with patch.object(coordinator, "_darwin_lib", return_value=UntrustedCwd()):
                    self.assertIsNone(coordinator._engine_project(processes, child.pid), row)
                foreign = self.foreign_engine([], observed)
                self.assertIsNone(foreign[0]["project"], row)
                self.assert_blocking(foreign, actual, True)
                self.assert_blocking(foreign, decoy, True)
        self.assertEqual(gate.request({"op": "ping"})["pid"], child.pid)

    def test_duplicate_native_path_selectors_fail_closed_and_block(self):
        first = self.project("first")
        second = self.project("second")
        for final in (first, second):
            with self.subTest(final=final):
                foreign = self.foreign_engine(
                    ["--path", str(first), "--path", str(final)], final)
                self.assertIsNone(foreign[0]["project"])
                self.assert_blocking(foreign, final, True)
                self.assert_blocking(foreign, self.tmp, True)

    def test_unsupported_inline_native_path_fails_closed_and_blocks(self):
        claimed = self.project("claimed")
        actual = self.project("actual")
        foreign = self.foreign_engine(["--path=" + str(claimed)], actual)
        self.assertIsNone(foreign[0]["project"])
        self.assert_blocking(foreign, actual, True)
        self.assert_blocking(foreign, claimed, True)

    def test_extra_native_project_selectors_cannot_hide_behind_a_claimed_path(self):
        claimed = self.project("claimed")
        actual = self.project("actual")
        (actual / "-other").mkdir()
        (actual / "-other" / "project.godot").write_text("[application]\n")
        for selector in (["--gdscript-docs", str(actual)], ["-other/project.godot"]):
            with self.subTest(selector=selector):
                foreign = self.foreign_engine(["--path", str(claimed), *selector], actual)
                self.assertIsNone(foreign[0]["project"])
                self.assert_blocking(foreign, actual, True)
                self.assert_blocking(foreign, claimed, True)

    def test_movie_operand_is_unknown_but_single_absolute_project_is_known(self):
        actual = self.project("actual")
        operand = self.project("movie-operand")
        project_file = str(operand / "project.godot")
        bare = self.foreign_engine([project_file], operand)
        self.assertEqual(bare[0]["project"], str(operand))
        self.assert_blocking(bare, operand, True)
        self.assert_blocking(bare, actual, False)
        before_parse = self.foreign_engine([project_file], actual)
        self.assertIsNone(before_parse[0]["project"])
        self.assert_blocking(before_parse, actual, True)
        self.assert_blocking(before_parse, operand, True)
        explicit = self.foreign_engine(["--headless", "--path", str(actual)], actual)
        self.assertEqual(explicit[0]["project"], str(actual))
        self.assert_blocking(explicit, actual, True)
        self.assert_blocking(explicit, operand, False)
        for arguments in (["--write-movie", project_file],
                          ["--path", str(actual), "--write-movie", project_file],
                          ["--headless", project_file]):
            with self.subTest(arguments=arguments):
                foreign = self.foreign_engine(arguments, actual)
                self.assertIsNone(foreign[0]["project"])
                self.assert_blocking(foreign, actual, True)
                self.assert_blocking(foreign, operand, True)

    def test_relative_native_path_is_unknown_before_or_after_native_chdir(self):
        before = self.project("before-chdir")
        target = self.project("before-chdir/target")
        # Both directories already contain real project files. The same native
        # argv can be observed before or after chdir, so neither root is proven.
        for cwd in (before, target):
            with self.subTest(cwd=cwd):
                foreign = self.foreign_engine(["--path", "target"], cwd)
                self.assertIsNone(foreign[0]["project"])
                self.assert_blocking(foreign, before, True)
                self.assert_blocking(foreign, target, True)

    def test_absolute_native_path_requires_matching_canonical_cwd_and_direct_project(self):
        actual = self.project("actual-cwd")
        claimed = self.project("claimed-path")
        for arguments in (["--path", str(claimed)],
                          ["--log-file", "--path", str(claimed)]):
            with self.subTest(arguments=arguments):
                foreign = self.foreign_engine(arguments, actual)
                self.assertIsNone(foreign[0]["project"])
                self.assert_blocking(foreign, actual, True)
                self.assert_blocking(foreign, claimed, True)
        alias = self.tmp / "claimed-alias"
        alias.symlink_to(claimed, target_is_directory=True)
        matching = self.foreign_engine(["--path", str(alias)], claimed)
        self.assertEqual(matching[0]["project"], str(claimed))
        self.assert_blocking(matching, claimed, True)
        self.assert_blocking(matching, actual, False)
        empty = self.tmp / "no-direct-project"
        empty.mkdir()
        for arguments in (["--path", str(empty)], [str(empty / "project.godot")]):
            with self.subTest(missing_project_arguments=arguments):
                foreign = self.foreign_engine(arguments, empty)
                self.assertIsNone(foreign[0]["project"])
                self.assert_blocking(foreign, empty, True)
                self.assert_blocking(foreign, actual, True)

    def test_native_upward_search_flags_make_otherwise_known_ownership_unknown(self):
        actual = self.project("upward-project")
        other = self.project("other-project")
        for flag in ("-u", "--upwards"):
            for arguments in ([flag], ["--headless", "--path", str(actual), flag]):
                with self.subTest(arguments=arguments):
                    foreign = self.foreign_engine(arguments, actual)
                    self.assertIsNone(foreign[0]["project"])
                    self.assert_blocking(foreign, actual, True)
                    self.assert_blocking(foreign, other, True)

    def test_cwd_ownership_requires_a_direct_project_without_ancestor_search(self):
        actual = self.project("parent-project")
        child = actual / "child-without-project"
        child.mkdir()
        inherited = self.foreign_engine([], child)
        self.assertIsNone(inherited[0]["project"])
        self.assert_blocking(inherited, child, True)
        self.assert_blocking(inherited, actual, True)
        direct = self.foreign_engine([], actual)
        self.assertEqual(direct[0]["project"], str(actual))
        self.assert_blocking(direct, actual, True)
        self.assert_blocking(direct, child, False)

    def test_native_user_argument_buckets_cannot_change_project_ownership(self):
        actual = self.project("actual")
        decoy = self.project("decoy")
        for separator in ("--", "++"):
            for native in ([], ["--path", str(actual)],
                           [str(actual / "project.godot")]):
                for user in (["--path", str(decoy)], ["--path=" + str(decoy)],
                             [str(decoy / "project.godot")],
                             ["--gdscript-docs", str(decoy)], ["-other/project.godot"],
                             ["--write-movie", str(decoy / "project.godot")],
                             ["-u"], ["--upwards"]):
                    with self.subTest(separator=separator, native=native, user=user):
                        foreign = self.foreign_engine([*native, separator, *user], actual)
                        self.assertEqual(foreign[0]["project"], str(actual))
                        self.assert_blocking(foreign, actual, True)
                        self.assert_blocking(foreign, decoy, False)


class DarwinGroupDeathTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("group_death_coordinator", COORDINATOR)
        self.coordinator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.coordinator)
        self.leader = 53123
        self.process = SimpleNamespace(pid=self.leader, returncode=None)
        self.identity = {"pid": self.leader, "start": "darwin:100:1"}
        self.members = {
            self.leader: {"status": 5, "pgid": self.leader, "ppid": os.getpid(),
                          "start_sec": 100, "start_usec": 1},
            self.leader + 1: {"status": 5, "pgid": self.leader, "ppid": self.leader,
                              "start_sec": 101, "start_usec": 2},
        }

    def proves_dead(self, snapshots=None, unreadable=(), truncated=False, process=None, identity=None):
        coordinator = self.coordinator
        snapshots = snapshots if snapshots is not None else [self.members, self.members]
        width = ctypes.sizeof(ctypes.c_int)

        class Kernel:
            def __init__(self):
                self.index = 0
                self.current = None

            def proc_listpids(self, kind, group, buffer, size):
                snapshot = snapshots[min(self.index, len(snapshots) - 1)]
                if buffer is None:
                    return (len(snapshot) + 1) * width
                self.current = snapshot
                self.index += 1
                for index, pid in enumerate(snapshot):
                    buffer[index] = pid
                return size if truncated else len(snapshot) * width

            def proc_pidinfo(self, pid, flavor, argument, output, size):
                if pid in unreadable or pid not in self.current:
                    return 0
                if self.current[pid]["status"] == 5 and argument != 1:
                    return 0  # Darwin does not expose zombies without this lookup mode.
                info = ctypes.cast(output, ctypes.POINTER(coordinator._BSDInfo)).contents
                info.pid = pid
                for field, value in self.current[pid].items():
                    setattr(info, field, value)
                return ctypes.sizeof(info)

        # Inject only kernel responses. Use the production ABI type, not a
        # copied BSD layout, and never inspect or signal any host process.
        with patch.object(coordinator, "_darwin_lib", return_value=Kernel()), \
                patch.object(coordinator, "sys", SimpleNamespace(platform="darwin")):
            return coordinator._darwin_owned_group_dead(
                self.process if process is None else process,
                self.identity if identity is None else identity)

    def test_unreadable_group_member_cannot_prove_group_death(self):
        self.assertFalse(self.proves_dead(unreadable={self.leader + 1}))

    @unittest.skipUnless(sys.platform == "darwin", "Requires the actual Darwin kernel")
    def test_actual_darwin_unreaped_zombie_group_is_proven_without_reaping(self):
        gate = Gate()
        self.addCleanup(gate.close)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GODOT_COORDINATOR_")}
        process = subprocess.Popen(
            gate.command(), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)

        def reap_owned_child():
            gate.release()
            try:
                process.communicate(timeout=WAIT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=WAIT)

        self.addCleanup(reap_owned_child)
        self.assertTrue(select.select([gate.listener], [], [], WAIT)[0],
                        "Owned child did not establish its kernel identity")
        identity = gate.accept()
        self.assertEqual(identity["pid"], process.pid)
        gate.release()
        gate.expect_closed(timeout=WAIT)
        deadline = time.monotonic() + WAIT
        terminal = None
        while terminal is None:
            # WNOWAIT establishes only the fixture's actual terminal state.
            # Production proof below must independently enumerate/read its
            # complete real kernel group and must not reap the reserved child.
            terminal = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if terminal is None:
                self.assertLess(time.monotonic(), deadline, "Owned child did not become a zombie")
                select.select([], [], [], 0.01)
        self.assertEqual(terminal.si_pid, process.pid)
        self.assertIsNone(process.returncode)
        self.assertTrue(self.coordinator._darwin_owned_group_dead(
            process, {"pid": identity["pid"], "start": identity["start"]}))
        self.assertIsNone(process.returncode)
        still_unreaped = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        self.assertIsNotNone(still_unreaped)
        self.assertEqual(still_unreaped.si_pid, process.pid)
        self.assertEqual(process.wait(timeout=WAIT), 0)

    @unittest.skipUnless(sys.platform == "darwin", "Requires the actual Darwin kernel")
    def test_transient_owned_group_permission_failure_waits_for_proof_without_resignalling(self):
        coordinator = self.coordinator
        library = coordinator._darwin_lib()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GODOT_COORDINATOR_")}
        for settles in (True, False):
            with self.subTest(kernel_transition_settles=settles):
                process = subprocess.Popen(
                    [sys.executable, "-c", "pass"], env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

                def reap_fixture(child=process):
                    try:
                        child.communicate(timeout=WAIT)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.communicate(timeout=WAIT)

                self.addCleanup(reap_fixture)
                ready_by = time.monotonic() + WAIT
                terminal = None
                while terminal is None:
                    terminal = os.waitid(
                        os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if terminal is None:
                        self.assertLess(time.monotonic(), ready_by, "Owned fixture did not exit")
                        select.select([], [], [], 0.01)
                info = coordinator._BSDInfo()
                size = ctypes.sizeof(info)
                self.assertEqual(library.proc_pidinfo(
                    process.pid, 3, 1, ctypes.byref(info), size), size)
                self.assertEqual((info.pid, info.ppid, info.pgid, info.status),
                                 (process.pid, os.getpid(), process.pid, 5))
                identity = {"pid": process.pid, "start": f"darwin:{info.start_sec}:{info.start_usec}"}
                clock = {"now": 10.0, "reads": 0}
                phase = {"settled": False}
                ownership = SimpleNamespace(deadline=10.3)
                original_deadline = ownership.deadline
                failure = PermissionError(errno.EPERM, "Controlled owned exit transition")
                signalled = False

                class TransitionKernel:
                    def proc_listpids(self, *args):
                        return library.proc_listpids(*args)

                    def proc_pidinfo(self, pid, flavor, argument, output, capacity):
                        used = library.proc_pidinfo(pid, flavor, argument, output, capacity)
                        if used == capacity and pid == process.pid and not phase["settled"]:
                            observed = ctypes.cast(output, ctypes.POINTER(coordinator._BSDInfo)).contents
                            observed.status = 2  # The observed transient is still SRUN.
                        return used

                def monotonic():
                    clock["reads"] += 1
                    if clock["reads"] > 1000:
                        raise AssertionError("Proof wait did not terminate on the bounded fixture clock")
                    return clock["now"]

                def advance_transition(delay):
                    self.assertGreaterEqual(delay, 0)
                    clock["now"] += delay
                    phase["settled"] = settles

                def deny_only_initial_signal(group, signum):
                    nonlocal signalled
                    self.assertEqual((group, signum), (process.pid, signal.SIGTERM))
                    self.assertFalse(signalled, "A proof-only wait attempted another group signal")
                    signalled = True
                    raise failure

                with patch.object(coordinator, "_darwin_lib", return_value=TransitionKernel()), \
                        patch.object(coordinator, "time", SimpleNamespace(
                            monotonic=monotonic, sleep=advance_transition)), \
                        patch.object(coordinator.os, "killpg", side_effect=deny_only_initial_signal):
                    if settles:
                        coordinator._cleanup_owned(ownership, process, identity=identity)
                    else:
                        with self.assertRaises(PermissionError) as raised:
                            coordinator._cleanup_owned(ownership, process, identity=identity)
                        self.assertIs(raised.exception, failure)
                self.assertEqual(ownership.deadline, original_deadline)
                self.assertLessEqual(clock["now"], original_deadline)
                if settles:
                    self.assertTrue(phase["settled"], "A live observation was accepted before transition")
                    self.assertEqual(process.returncode, 0, "Cleanup did not reap its actual Popen child")
                    with self.assertRaises(ChildProcessError):
                        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                else:
                    self.assertIsNone(process.returncode)
                    self.assertEqual(os.waitid(
                        os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT).si_pid, process.pid)

    def test_live_group_member_cannot_prove_group_death(self):
        live = {pid: dict(info) for pid, info in self.members.items()}
        live[self.leader + 1]["status"] = 2
        self.assertFalse(self.proves_dead(snapshots=[live, live]))

    def test_changed_group_birth_identity_cannot_prove_group_death(self):
        changed = {pid: dict(info) for pid, info in self.members.items()}
        changed[self.leader + 1]["start_usec"] += 1
        self.assertFalse(self.proves_dead(snapshots=[self.members, changed]))

    def test_complete_zombie_group_requires_exact_unreaped_owned_leader(self):
        self.assertTrue(self.proves_dead())
        self.assertFalse(self.proves_dead(truncated=True))
        self.assertFalse(self.proves_dead(process=SimpleNamespace(pid=self.leader, returncode=0)))
        self.assertFalse(self.proves_dead(identity={"pid": self.leader, "start": "darwin:999:1"}))
        foreign_parent = {pid: dict(info) for pid, info in self.members.items()}
        foreign_parent[self.leader]["ppid"] = os.getpid() + 1
        self.assertFalse(self.proves_dead(snapshots=[foreign_parent, foreign_parent]))
        foreign_group = {pid: dict(info) for pid, info in self.members.items()}
        foreign_group[self.leader + 1]["pgid"] += 1
        self.assertFalse(self.proves_dead(snapshots=[foreign_group, foreign_group]))


class GodotCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="godot-coordinator-test-")
        self.tmp = Path(self.temporary.name)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("GODOT_COORDINATOR_")}
        self.env["GODOT_COORDINATOR_STATE_DIR"] = str(self.tmp / "state")
        self.processes = []
        self.gates = []
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        # Only touch children launched by this harness. Releasing all connected
        # gates also stops survivors when a coordinator has deliberately died.
        for gate in self.gates:
            gate.release()
            if gate.connection is None:
                # Reset pending connections and refuse late admissions too.
                gate.listener.close()
        for process in self.processes:
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=2)
        for gate in self.gates:
            gate.close()

    def project(self, name):
        path = self.tmp / name
        path.mkdir()
        return path

    def gate(self):
        result = Gate()
        self.gates.append(result)
        return result

    def hold_state_lock(self):
        snapshot = self.status()
        self.assertEqual(snapshot["domain"], "isolated", "Never lock the production domain")
        state_dir = Path(snapshot["state_dir"]).resolve()
        self.assertIn(self.tmp.resolve(), state_dir.parents, "Never lock production coordinator state")
        self.assertEqual(state_dir, Path(self.env["GODOT_COORDINATOR_STATE_DIR"]).resolve())
        gate = self.gate()
        process = subprocess.Popen(
            gate.command(), env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(process)
        self.admitted(gate, process)
        lock = state_dir / "lock"
        self.assertEqual(gate.request({"op": "lock", "path": str(lock)}), {"locked": True})
        return process, gate, lock

    def command(self, mode, project, purpose, command, timeout=15):
        argv = [sys.executable, str(COORDINATOR), "run", "--mode", mode,
                "--timeout", str(timeout), "--purpose", purpose]
        if project is not None:
            argv.extend(["--project", str(project)])
        return [*argv, "--", *command]

    def start(self, mode, project, purpose, gate=None, timeout=15, extra_env=None, command=None):
        gate = gate or self.gate()
        env = dict(self.env)
        env.update(extra_env or {})
        process = subprocess.Popen(
            self.command(mode, project, purpose, command or gate.command(), timeout),
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(process)
        return process, gate

    def start_work_cutoff(self, phase, project, gate):
        control = self.gate()
        host, port = control.listener.getsockname()
        read_fd, write_fd = os.pipe()
        readiness = os.fdopen(read_fd, "rb")
        self.addCleanup(readiness.close)
        argv = self.command("capture", project, f"{phase}-cutoff",
                            gate.command(notify_term_at=True), timeout=2)[2:]
        separator = argv.index("--")
        argv[separator:separator] = ["--ready-fd", str(write_fd)]
        try:
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", WORK_CUTOFF_COMMAND,
                 str(COORDINATOR.parent), phase, host, str(port), control.nonce, *argv],
                env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, pass_fds=(write_fd,), start_new_session=True)
        finally:
            os.close(write_fd)
        self.processes.append(process)
        return process, control, readiness

    def admitted(self, gate, process, diagnostics=False):
        deadline = time.monotonic() + WAIT
        while not gate.ready():
            code = process.poll()
            detail = self.admission_diagnostics(process) if diagnostics and code is not None else ""
            self.assertIsNone(code, f"Coordinator exited before command admission: {code} {detail}")
            remaining = deadline - time.monotonic()
            detail = self.admission_diagnostics(process) if diagnostics and remaining <= 0 else ""
            self.assertGreater(remaining, 0, f"Command was never admitted {detail}")
            select.select([gate.listener], [], [], min(0.025, remaining))
        return gate.accept()

    def admission_diagnostics(self, process):
        details = {"blocked_stderr": ""}
        if process.stderr is not None and select.select([process.stderr], [], [], 0)[0]:
            details["blocked_stderr"] = os.read(process.stderr.fileno(), 8192).decode(errors="replace")
        try:
            result = subprocess.run(
                [sys.executable, str(COORDINATOR), "status", "--json"], env=self.env,
                capture_output=True, text=True, timeout=1)
            if result.returncode == 0:
                snapshot = json.loads(result.stdout)
                details["active"] = snapshot.get("active", [])
                details["waiting"] = snapshot.get("waiting", [])
                details["orphans"] = [record.get("orphans", []) for record in details["active"]]
            else:
                details["status_error"] = result.stderr
        except (subprocess.TimeoutExpired, ValueError, OSError) as error:
            details["status_error"] = str(error)
        return json.dumps(details, sort_keys=True)

    def status(self, env=None):
        result = subprocess.run([sys.executable, str(COORDINATOR), "status", "--json"],
                                env=self.env if env is None else env,
                                capture_output=True, text=True, timeout=WAIT)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertIsInstance(snapshot["active"], list)
        self.assertIsInstance(snapshot["waiting"], list)
        return snapshot

    def wait_record(self, group, purpose):
        deadline = time.monotonic() + WAIT
        while True:
            snapshot = self.status()
            records = [record for record in snapshot[group] if record["purpose"] == purpose]
            if records:
                self.assertEqual(len(records), 1, snapshot)
                return records[0]
            self.assertLess(time.monotonic(), deadline,
                            f"No {group} record for {purpose}: {snapshot}")
            time.sleep(0.01)

    def not_admitted(self, gate):
        self.assertIsNone(gate.connection)
        self.assertFalse(gate.ready(), "A blocked command actually began execution")

    def finish(self, process, gate):
        gate.release()
        self.assertEqual(process.wait(timeout=WAIT), 0)

    def assert_idle(self):
        deadline = time.monotonic() + WAIT
        while True:
            snapshot = self.status()
            if not snapshot["active"] and not snapshot["waiting"]:
                return
            self.assertLess(time.monotonic(), deadline, f"Leaked coordinator claims: {snapshot}")
            time.sleep(0.01)

    def test_private_json_publication_hides_uncommitted_file_and_never_clobbers(self):
        directory = self.tmp / "private-publication"
        directory.mkdir(mode=0o700)
        target = directory / "receipt.json"
        payload = {"api": 1, "purpose": "publication", "marker": os.urandom(16).hex()}
        gate = self.gate()
        host, port = gate.listener.getsockname()
        publisher = subprocess.Popen(
            [sys.executable, "-u", "-c", ATOMIC_PUBLICATION_COMMAND,
             str(COORDINATOR.parent), str(target), host, str(port), gate.nonce, json.dumps(payload)],
            env=self.env, cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(publisher)
        self.admitted(gate, publisher)
        self.assertEqual(gate.request({"op": "publish"}), {"event": "created-before-body"})
        self.assertFalse(target.exists(), "A final receipt became visible before its body was written")
        self.assertEqual(gate.request({"op": "resume"}), {"event": "published"})

        result = subprocess.run(
            [sys.executable, "-u", "-c", PRIVATE_JSON_READER_COMMAND,
             str(COORDINATOR.parent), str(target)],
            env=self.env, capture_output=True, text=True, timeout=WAIT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), payload)
        before = target.stat()
        original_bytes = target.read_bytes()
        self.assertEqual(gate.request({"op": "retry", "value": {"must": "not overwrite"}}),
                         {"refused": True})
        after = target.stat()
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertEqual(target.read_bytes(), original_bytes)
        self.finish(publisher, gate)
        self.assert_idle()

    def test_production_status_namespace_ignores_home_xdg_and_domain_environment(self):
        identities = []
        for case in ("first", "second"):
            env = {key: value for key, value in self.env.items()
                   if not key.startswith("GODOT_COORDINATOR_")}
            for variable in ("HOME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
                directory = self.tmp / f"{case}-{variable.lower()}"
                directory.mkdir(mode=0o700)
                env[variable] = str(directory)
            env["GODOT_COORDINATOR_DOMAIN"] = f"forged-domain-{case}"
            snapshot = self.status(env=env)
            state_dir = Path(snapshot["state_dir"])
            self.assertTrue(state_dir.is_absolute())
            self.assertEqual(state_dir.resolve().parent, Path("/var/tmp").resolve())
            self.assertNotIn(self.tmp, state_dir.resolve().parents)
            self.assertIsInstance(snapshot["boot_id"], str)
            self.assertTrue(snapshot["boot_id"])
            identities.append((str(state_dir.resolve()), snapshot["boot_id"]))
        self.assertEqual(identities[0], identities[1])

    def test_real_process_metadata_reads_project_arguments_and_ownership_environment(self):
        project = self.project("metadata project")
        (project / "project.godot").write_text("[application]\n")
        alias = self.tmp / "metadata project alias"
        alias.symlink_to(project, target_is_directory=True)
        gate = self.gate()
        token = "metadata-witness-" + os.urandom(16).hex()
        marker = os.urandom(8).hex()
        env = dict(self.env)
        env["GODOT_COORDINATOR_TOKEN"] = token
        env["PYTHONUNBUFFERED"] = "1"
        # Python's bootstrap -u means unbuffered, but the engine metadata
        # parser correctly treats that same native switch as upward search.
        # Keep it out of this target's bootstrap, not out of engine arguments.
        bootstrap = [argument for argument in gate.command() if argument != "-u"]
        application_arguments = ["--path", str(alias), "--metadata-witness", marker]
        child = subprocess.Popen(
            [*bootstrap, *application_arguments], env=env, cwd=project, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(child)
        self.assertEqual(self.admitted(gate, child)["pid"], child.pid)
        result = subprocess.run(
            [sys.executable, "-u", "-c", PROCESS_METADATA_COMMAND,
             str(COORDINATOR.parent), str(child.pid)],
            env=self.env, capture_output=True, text=True, timeout=WAIT)
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(result.stdout)
        self.assertIsInstance(metadata["arguments"], list, metadata["inspection_error"])
        self.assertEqual(metadata["arguments"][-len(application_arguments):], application_arguments)
        self.assertEqual(metadata["project"], str(project.resolve()))
        self.assertEqual(metadata["token"], token)
        self.finish(child, gate)
        self.assert_idle()

    def test_distinct_project_roots_execute_concurrently(self):
        first, first_gate = self.start("shared", self.project("first"), "first-root")
        self.admitted(first_gate, first)
        second, second_gate = self.start("shared", self.project("second"), "second-root")
        self.admitted(second_gate, second)
        self.assertIsNone(first.poll(), "First project ended before overlap was observed")
        self.assertEqual({record["purpose"] for record in self.status()["active"]},
                         {"first-root", "second-root"})
        self.finish(second, second_gate)
        self.finish(first, first_gate)
        self.assert_idle()

    def test_same_canonical_project_serializes_symlink_alias(self):
        project = self.project("real-project")
        alias = self.tmp / "alias"
        alias.symlink_to(project, target_is_directory=True)
        first, first_gate = self.start("shared", project, "canonical-owner")
        self.admitted(first_gate, first)
        second, second_gate = self.start("shared", alias, "alias-waiter")
        waiting = self.wait_record("waiting", "alias-waiter")
        self.assertEqual(waiting["project"], str(project.resolve()))
        self.not_admitted(second_gate)
        self.finish(first, first_gate)
        self.admitted(second_gate, second)
        self.finish(second, second_gate)
        self.assert_idle()

    def test_two_capture_batches_run_and_third_waits(self):
        first, first_gate = self.start("capture", self.project("one"), "capture-one")
        self.admitted(first_gate, first)
        second, second_gate = self.start("capture", self.project("two"), "capture-two")
        self.admitted(second_gate, second)
        third, third_gate = self.start("capture", self.project("three"), "capture-three")
        self.assertEqual(self.wait_record("waiting", "capture-three")["mode"], "capture")
        self.not_admitted(third_gate)
        self.finish(first, first_gate)
        self.admitted(third_gate, third)
        self.assertIsNone(second.poll())
        self.finish(third, third_gate)
        self.finish(second, second_gate)
        self.assert_idle()

    def test_exclusive_waiter_closes_new_shared_admission_until_drain(self):
        first, first_gate = self.start("shared", self.project("one"), "draining-one")
        self.admitted(first_gate, first)
        second, second_gate = self.start("shared", self.project("two"), "draining-two")
        self.admitted(second_gate, second)
        exclusive, exclusive_gate = self.start("exclusive", self.project("measurement"), "exclusive")
        self.wait_record("waiting", "exclusive")
        newcomer, newcomer_gate = self.start("shared", self.project("new"), "new-shared")
        self.wait_record("waiting", "new-shared")
        self.not_admitted(exclusive_gate)
        self.not_admitted(newcomer_gate)
        self.finish(first, first_gate)
        self.wait_record("waiting", "exclusive")
        self.not_admitted(exclusive_gate)
        self.not_admitted(newcomer_gate)
        self.finish(second, second_gate)
        self.admitted(exclusive_gate, exclusive)
        self.wait_record("waiting", "new-shared")
        self.not_admitted(newcomer_gate)
        self.finish(exclusive, exclusive_gate)
        self.admitted(newcomer_gate, newcomer)
        self.finish(newcomer, newcomer_gate)
        self.assert_idle()

    def test_internal_machine_denial_keeps_status_and_owner_progress_without_skipping_fresh_scans(self):
        # Only contender interpreters replace discovery. The shared owner and
        # every public status consumer execute the untouched coordinator CLI.
        bootstrap = r"""
import json
import os
import socket
import sys
import time
from pathlib import Path

runtime, controls, host, port, nonce, *argv = sys.argv[1:]
sys.path.insert(0, runtime)
import godot_coordinator as core

controls = Path(controls)
blocker = json.loads((controls / "blocker.json").read_text())
original_foreign, original_legacy = core._foreign, core._legacy
first_scan = True
def foreign(data, processes, domain):
    global first_scan
    discovered = original_foreign(data, processes, domain)
    if first_scan:
        first_scan = False
        with socket.create_connection((host, int(port)), timeout=8) as event:
            event.sendall(json.dumps({"nonce": nonce, "pid": os.getpid()}).encode() + b"\n")
            while not (controls / "scan-open").exists():
                time.sleep(0.01)
    if not (controls / "foreign-clear").exists():
        discovered.append({
            **core._identity(processes.items[blocker["pid"]]),
            "binary": processes.executable(blocker["pid"]), "project": None,
            "kind": "engine", "capture_batch": False,
            "reason": "controlled unknown-project foreign owner"})
    return discovered

def legacy(processes, domain):
    discovered = original_legacy(processes, domain)
    if not (controls / "legacy-clear").exists():
        discovered.append({
            "path": str(controls / "legacy-owner"),
            **core._identity(processes.items[blocker["pid"]]),
            "reason": "controlled live legacy timing-exclusive owner"})
    return discovered

core._foreign, core._legacy = foreign, legacy
sys.exit(core.main(argv))
"""
        controls = self.tmp / "contender-discovery"
        controls.mkdir()
        payloads = []
        owned = []

        def status():
            consumer = subprocess.Popen(
                [sys.executable, str(COORDINATOR), "status", "--json"],
                env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            self.processes.append(consumer)
            owned.append(consumer)
            stdout, stderr = consumer.communicate(timeout=3)
            self.assertEqual(consumer.returncode, 0, stderr)
            snapshot = json.loads(stdout)
            self.assertEqual(snapshot["domain"], "isolated")
            self.assertEqual(Path(snapshot["state_dir"]).resolve(),
                             Path(self.env["GODOT_COORDINATOR_STATE_DIR"]).resolve())
            return snapshot

        def waiting(purpose, predicate=lambda record: True):
            deadline = time.monotonic() + WAIT
            while True:
                snapshot = status()
                records = [record for record in snapshot["waiting"]
                           if record["purpose"] == purpose]
                if records and predicate(records[0]):
                    self.assertEqual(len(records), 1, snapshot)
                    return snapshot, records[0]
                self.assertLess(time.monotonic(), deadline, snapshot)
                time.sleep(0.01)

        def committed_denial(kinds):
            # Public status deliberately recomputes discovery in its own
            # unmodified interpreter. Its isolated foreign/legacy lists are
            # empty, so use publication only to synchronize this fixture's
            # external-denial phase, never as the sole admission assertion.
            deadline = time.monotonic() + WAIT
            while True:
                records = json.loads(state_file.read_text())["records"]
                record = next(record for record in records
                              if record["purpose"] == "scan-progress-exclusive")
                blockers = record["blockers"]
                if (record["status"] == "waiting" and blockers and
                        {item["kind"] for item in blockers} == kinds and
                        all(item.get("pid") == blocker_identity["pid"] and
                            item.get("start") == blocker_identity["start"]
                            for item in blockers)):
                    return
                self.assertIsNone(exclusive.poll())
                self.assertLess(time.monotonic(), deadline, record)
                time.sleep(0.01)

        def contender(mode, purpose):
            payload, event = self.gate(), self.gate()
            payloads.append(payload)
            host, port = event.listener.getsockname()
            argv = self.command(mode, self.project(purpose), purpose, payload.command())[2:]
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", bootstrap, str(COORDINATOR.parent),
                 str(controls), host, str(port), event.nonce, *argv],
                env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.processes.append(process)
            owned.append(process)
            return process, payload, event

        try:
            blocker_gate = self.gate()
            payloads.append(blocker_gate)
            blocker_process = subprocess.Popen(
                blocker_gate.command(), env=self.env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            self.processes.append(blocker_process)
            owned.append(blocker_process)
            blocker_identity = self.admitted(blocker_gate, blocker_process)
            (controls / "blocker.json").write_text(json.dumps({
                key: blocker_identity[key] for key in ("pid", "start", "binary")}))
            holder, holder_gate = self.start(
                "shared", self.project("scan-progress-holder"), "scan-progress-holder")
            owned.append(holder)
            payloads.append(holder_gate)
            holder_identity = self.admitted(holder_gate, holder)
            exclusive, exclusive_gate, exclusive_scan = contender(
                "exclusive", "scan-progress-exclusive")
            # Observe atomic publication only to order the two launches. Do not
            # take flock: the regression itself may already hold it in discovery.
            deadline = time.monotonic() + WAIT
            state_file = Path(self.env["GODOT_COORDINATOR_STATE_DIR"]) / "state.json"
            while not any(record["purpose"] == "scan-progress-exclusive"
                          for record in json.loads(state_file.read_text())["records"]):
                self.assertIsNone(exclusive.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            late, late_gate, _ = contender("shared", "scan-progress-late")
            snapshot, late_record = waiting(
                "scan-progress-late",
                lambda record: any(blocker.get("purpose") == "scan-progress-exclusive"
                                   for blocker in record["blockers"]))
            self.assertEqual([record["purpose"] for record in snapshot["active"]],
                             ["scan-progress-holder"])
            self.assertEqual({record["purpose"] for record in snapshot["waiting"]},
                             {"scan-progress-exclusive", "scan-progress-late"})
            exclusive_record = next(record for record in snapshot["waiting"]
                                    if record["purpose"] == "scan-progress-exclusive")
            self.assertLess(exclusive_record["sequence"], late_record["sequence"])
            self.assertEqual(holder_gate.request({"op": "ping"})["pid"], holder_identity["pid"])
            self.not_admitted(exclusive_gate)
            self.not_admitted(late_gate)

            # A denied poll must not hold the transaction lock against owner
            # heartbeat/finalization. Keep discovery closed through this wait.
            holder_gate.release()
            self.assertEqual(holder.wait(timeout=3), 0)
            holder_gate.expect_closed()
            self.admitted(exclusive_scan, exclusive)
            self.not_admitted(exclusive_gate)
            self.not_admitted(late_gate)
            (controls / "scan-open").touch()

            committed_denial({"engine", "legacy"})
            snapshot, record = waiting("scan-progress-exclusive")
            self.assertFalse(snapshot["active"], snapshot)
            self.assertEqual(record["owner"]["pid"], exclusive.pid)
            self.assertIsNone(record["child"])
            self.assertEqual(blocker_gate.request({"op": "ping"})["pid"],
                             blocker_identity["pid"])
            self.not_admitted(exclusive_gate)
            self.not_admitted(late_gate)
            (controls / "foreign-clear").touch()
            committed_denial({"legacy"})
            snapshot, record = waiting("scan-progress-exclusive")
            self.assertFalse(snapshot["active"], snapshot)
            self.assertEqual(record["owner"]["pid"], exclusive.pid)
            self.assertIsNone(record["child"])
            self.assertEqual(blocker_gate.request({"op": "ping"})["pid"],
                             blocker_identity["pid"])
            self.not_admitted(exclusive_gate)
            self.not_admitted(late_gate)
            (controls / "legacy-clear").touch()
            exclusive_identity = self.admitted(exclusive_gate, exclusive)
            snapshot, _ = waiting("scan-progress-late")
            self.assertEqual([record["purpose"] for record in snapshot["active"]],
                             ["scan-progress-exclusive"])
            self.assertEqual(exclusive_gate.request({"op": "ping"})["pid"],
                             exclusive_identity["pid"])
            self.not_admitted(late_gate)
            self.finish(exclusive, exclusive_gate)
            late_identity = self.admitted(late_gate, late)
            self.assertNotEqual(late_identity["pid"], exclusive_identity["pid"])
            self.assertNotEqual(late_identity["pid"], holder_identity["pid"])
            snapshot = status()
            self.assertEqual([record["purpose"] for record in snapshot["active"]],
                             ["scan-progress-late"])
            self.assertFalse(snapshot["waiting"], snapshot)
            self.finish(late, late_gate)
            exclusive_gate.expect_closed()
            late_gate.expect_closed()
            self.assertIsNone(blocker_process.poll())
            self.finish(blocker_process, blocker_gate)
            blocker_gate.expect_closed()
            self.assert_idle()
        finally:
            # Unblock discovery and release actual commands before cancellation;
            # never remove private state while a child still uses it.
            for name in ("scan-open", "foreign-clear", "legacy-clear"):
                (controls / name).touch()
            for gate in payloads:
                if gate.connection is None and gate.ready():
                    gate.accept()
                gate.release()
            for process in owned:
                try:
                    process.communicate(timeout=WAIT)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.communicate(timeout=WAIT)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=WAIT)

    def test_nonzero_exit_propagates_and_releases_project(self):
        project = self.project("failure")
        failed, _ = self.start("shared", project, "nonzero", command=[
            sys.executable, "-c", "import sys; sys.exit(23)"])
        self.assertEqual(failed.wait(timeout=WAIT), 23)
        successor, successor_gate = self.start("shared", project, "after-nonzero")
        self.admitted(successor_gate, successor)
        self.finish(successor, successor_gate)
        self.assert_idle()

    def test_completed_child_keeps_actual_exit_status_in_cleanup_reserve(self):
        project = self.project("late-cleanup")
        process, gate = self.start("shared", project, "cleanup-reserve", timeout=4)
        self.admitted(gate, process)
        deadline = self.wait_record("active", "cleanup-reserve")["deadline"]
        locker, lock_gate, lock = self.hold_state_lock()
        gate.connection.sendall(b'{"op":"exit","code":23}\n')
        gate.expect_closed(timeout=WAIT)
        # The child has finished. Hold only our isolated transaction-lock fd,
        # releasing in the final 150 ms of the caller's observed real deadline.
        self.assertGreater(deadline - time.monotonic(), 0.3)
        self.assertEqual(lock_gate.request({"op": "unlock-at", "path": str(lock),
                                           "at": deadline - 0.15}), {"unlocked": True})
        self.assertEqual(process.wait(timeout=WAIT), 23)
        self.finish(locker, lock_gate)
        successor, successor_gate = self.start("shared", project, "after-late-cleanup")
        self.admitted(successor_gate, successor)
        self.finish(successor, successor_gate)
        self.assert_idle()

    def test_queued_timeout_removes_exclusive_barrier_without_starting_command(self):
        holder, holder_gate = self.start("shared", self.project("holder"), "holder")
        self.admitted(holder_gate, holder)
        started = time.monotonic()
        expired, expired_gate = self.start("exclusive", None, "expired-exclusive", timeout=2)
        self.wait_record("waiting", "expired-exclusive")
        newcomer, newcomer_gate = self.start("shared", self.project("new"), "after-expiry")
        self.wait_record("waiting", "after-expiry")
        self.not_admitted(newcomer_gate)
        self.assertNotEqual(expired.wait(timeout=WAIT), 0)
        self.assertLess(time.monotonic() - started, 3.5)
        self.not_admitted(expired_gate)
        self.admitted(newcomer_gate, newcomer)
        self.assertIsNone(holder.poll(), "Queue cleanup incorrectly terminated another owner")
        snapshot = self.status()
        self.assertNotIn("expired-exclusive",
                         [record["purpose"] for record in snapshot["waiting"] + snapshot["active"]])
        self.finish(newcomer, newcomer_gate)
        self.finish(holder, holder_gate)
        self.assert_idle()

    def test_python_lease_cancellation_removes_queued_exclusive_barrier(self):
        holder, holder_gate = self.start("shared", self.project("holder"), "uncancelled-holder")
        self.admitted(holder_gate, holder)
        cancellation = self.tmp / "cancel-exclusive"
        cancelled_gate = self.gate()
        cancelled = subprocess.Popen(
            [sys.executable, "-u", "-c", CANCELLABLE_LEASE_COMMAND,
             str(COORDINATOR.parent), str(cancellation), json.dumps(cancelled_gate.command())],
            env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(cancelled)
        self.wait_record("waiting", "cancelled-python-lease")
        newcomer, newcomer_gate = self.start("shared", self.project("new"), "after-python-cancel")
        self.wait_record("waiting", "after-python-cancel")
        self.not_admitted(cancelled_gate)
        self.not_admitted(newcomer_gate)
        cancellation.touch()
        self.assertEqual(cancelled.wait(timeout=2), 42)
        self.not_admitted(cancelled_gate)
        self.admitted(newcomer_gate, newcomer)
        self.assertIsNone(holder.poll(), "Cancellation terminated an unrelated lease holder")
        snapshot = self.status()
        self.assertNotIn("cancelled-python-lease",
                         [record["purpose"] for record in snapshot["active"] + snapshot["waiting"]])
        self.finish(newcomer, newcomer_gate)
        self.finish(holder, holder_gate)
        self.assert_idle()

    def test_python_deadline_and_cancellation_interrupt_held_transaction_lock(self):
        locker, lock_gate, _ = self.hold_state_lock()
        for mode, timeout, expected_code in (("deadline", 0.75, 43), ("cancel", 15, 42)):
            event_gate, payload_gate = self.gate(), self.gate()
            host, port = event_gate.listener.getsockname()
            cancellation = self.tmp / f"blocked-{mode}-cancel"
            probe = subprocess.Popen(
                [sys.executable, "-u", "-c", BLOCKED_LEASE_COMMAND,
                 str(COORDINATOR.parent), str(cancellation), str(self.project(f"blocked-{mode}")),
                 json.dumps(payload_gate.command()), host, str(port), event_gate.nonce, str(timeout)],
                env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.processes.append(probe)
            self.admitted(event_gate, probe)
            if mode == "cancel":
                cancellation.touch()
            self.assertEqual(probe.wait(timeout=2), expected_code, mode)
            self.not_admitted(payload_gate)
            self.assertEqual(lock_gate.request({"op": "ping"})["pid"], locker.pid)
        self.finish(locker, lock_gate)
        self.assert_idle()

    def test_execution_timeout_cleans_owned_command_and_its_descendant(self):
        project = self.project("timed-out")
        descendant_gate = self.gate()
        started = time.monotonic()
        expired, expired_gate = self.start("shared", project, "execution-timeout", timeout=2)
        self.admitted(expired_gate, expired)
        descendant = expired_gate.spawn(descendant_gate.command())
        self.admitted(descendant_gate, descendant)
        self.assertNotEqual(expired.wait(timeout=WAIT), 0)
        self.assertLess(time.monotonic() - started, 3.5)
        expired_gate.expect_closed()
        descendant_gate.expect_closed()
        successor, successor_gate = self.start("shared", project, "after-execution-timeout")
        self.admitted(successor_gate, successor)
        self.finish(successor, successor_gate)
        self.assert_idle()

    def assert_work_lock_cutoff(self, phase):
        survivor, survivor_gate = self.start(
            "shared", self.project(f"{phase}-survivor"), f"{phase}-survivor")
        survivor_identity = self.admitted(survivor_gate, survivor)
        project = self.project(f"{phase}-cutoff")
        payload = self.gate()
        started = time.monotonic()
        expired, control, readiness = self.start_work_cutoff(phase, project, payload)
        event = self.admitted(control, expired)
        self.assertEqual(event["phase"], phase)
        deadline = event["deadline"]
        if phase == "heartbeat":
            identity = self.admitted(payload, expired)
            self.assertEqual(identity["pid"], event["helper_pid"])
        else:
            self.not_admitted(payload)
        locker, lock_gate, lock = self.hold_state_lock()
        unlock_pending = False
        try:
            self.assertEqual(survivor_gate.request({"op": "ping"})["pid"],
                             survivor_identity["pid"])
            self.assertGreater(deadline - 0.35, time.monotonic(),
                               "Lock fixture must be armed before the controlled work attempt")
            unlock_at = deadline - 0.15
            # Gate.request would block until the holder releases. Send now and
            # read its ACK later, leaving this controller free to observe TERM.
            lock_gate.connection.sendall(json.dumps({
                "op": "unlock-at", "path": str(lock), "at": unlock_at}).encode() + b"\n")
            unlock_pending = True
            control.connection.sendall(b'{"op":"resume"}\n')
            if phase == "heartbeat":
                payload.connection.settimeout(max(0.001, unlock_at - time.monotonic()))
                term = json.loads(payload.reader.readline())
                self.assertEqual(term["event"], "term")
                self.assertLess(term["at"], unlock_at,
                                "Heartbeat spent cleanup reserve waiting for the lock")
                payload.connection.settimeout(WAIT)
            acknowledgement = json.loads(lock_gate.reader.readline())
            unlock_pending = False
            self.assertEqual(acknowledgement, {"unlocked": True})
        finally:
            try:
                if unlock_pending:
                    # The holder's real bounded release remains armed even
                    # when the pre-unlock TERM assertion fails.
                    self.assertEqual(json.loads(lock_gate.reader.readline()), {"unlocked": True})
            finally:
                # Releasing this standalone fixture also closes its lock fd
                # if setup failed before the timed release was armed.
                self.finish(locker, lock_gate)

        code = expired.wait(timeout=3.5)
        stderr = ""
        if select.select([expired.stderr], [], [], 0)[0]:
            stderr = os.read(expired.stderr.fileno(), 8192).decode(errors="replace")
        self.assertEqual(code, 124, stderr)
        self.assertLess(time.monotonic() - started, 3.5)
        if phase == "registration":
            self.assertEqual(readiness.read(), b"",
                             "Command readiness was announced after its work cutoff")
            self.not_admitted(payload)
        else:
            self.assertEqual(json.loads(readiness.read())["pid"], identity["pid"])
            payload.expect_closed()
        with self.assertRaises(ProcessLookupError, msg="Owned cutoff child was not reaped"):
            os.kill(event["helper_pid"], 0)
        self.assertIsNone(survivor.poll(), "Work timeout disturbed an unrelated owner")
        self.assertEqual(survivor_gate.request({"op": "ping"})["pid"], survivor_identity["pid"])
        snapshot = self.status()
        self.assertEqual({record["purpose"] for record in snapshot["active"]},
                         {f"{phase}-survivor"})
        self.assertEqual(snapshot["waiting"], [])
        successor, successor_gate = self.start("capture", project, f"after-{phase}-cutoff")
        self.admitted(successor_gate, successor)
        self.finish(successor, successor_gate)
        self.finish(survivor, survivor_gate)
        self.assert_idle()

    def test_work_heartbeat_lock_wait_cannot_spend_owned_cleanup_reserve(self):
        self.assert_work_lock_cutoff("heartbeat")

    def test_work_registration_lock_wait_never_announces_command_after_work_cutoff(self):
        self.assert_work_lock_cutoff("registration")

    def test_timeout_kills_and_reaps_term_ignoring_child_without_disturbing_other_owner(self):
        survivor, survivor_gate = self.start(
            "shared", self.project("unrelated-survivor"), "unrelated-survivor")
        survivor_identity = self.admitted(survivor_gate, survivor)
        project = self.project("stubborn-timeout")
        stubborn_gate = self.gate()
        started = time.monotonic()
        expired, _ = self.start(
            "capture", project, "stubborn-timeout", gate=stubborn_gate, timeout=2,
            command=stubborn_gate.command(ignore_term=True))
        identity = self.admitted(stubborn_gate, expired)
        code = expired.wait(timeout=3.5)
        stderr = ""
        if code != 124 and select.select([expired.stderr], [], [], 0)[0]:
            stderr = os.read(expired.stderr.fileno(), 8192).decode(errors="replace")
        self.assertEqual(code, 124, stderr)
        self.assertLess(time.monotonic() - started, 3.5)
        try:
            stubborn_gate.expect_closed()
        except AssertionError as error:
            if select.select([expired.stderr], [], [], 0)[0]:
                stderr += os.read(expired.stderr.fileno(), 8192).decode(errors="replace")
            self.fail(f"{error}; actual coordinator stderr: {stderr!r}")
        with self.assertRaises(ProcessLookupError, msg="Owned stubborn child was not reaped"):
            os.kill(identity["pid"], 0)
        self.assertIsNone(survivor.poll(), "Timeout cleanup disturbed an unrelated lease owner")
        self.assertEqual(survivor_gate.request({"op": "ping"})["pid"], survivor_identity["pid"])
        snapshot = self.status()
        self.assertEqual({record["purpose"] for record in snapshot["active"]}, {"unrelated-survivor"})
        self.assertEqual(snapshot["waiting"], [])
        successor, successor_gate = self.start("capture", project, "after-stubborn-timeout")
        self.admitted(successor_gate, successor)
        self.finish(successor, successor_gate)
        self.finish(survivor, survivor_gate)
        self.assert_idle()

    def wait_owned_payload_terminal(self, identity, parent_pid, deadline):
        """Independently observe the exact unreaped child without Popen.poll/wait."""
        pid, birth = identity["pid"], identity["start"]
        end = min(deadline, time.monotonic() + WAIT)
        if sys.platform == "darwin":
            class BsdInfo(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint32) for name in (
                    "flags", "status", "xstatus", "pid", "ppid")]
                _fields_ += [("credentials", ctypes.c_uint32 * 7),
                             ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                             ("counts", ctypes.c_uint32 * 5), ("nice", ctypes.c_int32),
                             ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]

            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            query = library.proc_pidinfo
            query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                              ctypes.c_void_p, ctypes.c_int]
            query.restype = ctypes.c_int

            def terminal():
                info = BsdInfo()
                size = ctypes.sizeof(info)
                return (query(pid, 3, 1, ctypes.byref(info), size) == size
                        and info.pid == pid and info.ppid == parent_pid and info.status == 5
                        and f"darwin:{info.start_sec}:{info.start_usec}" == birth)
        elif sys.platform.startswith("linux"):
            def terminal():
                try:
                    text = Path(f"/proc/{pid}/stat").read_text()
                    fields = text[text.rfind(")") + 2:].split()
                    return (int(text[:text.find("(")].strip()) == pid and fields[0] == "Z"
                            and int(fields[1]) == parent_pid and "linux:" + fields[19] == birth)
                except (OSError, ValueError, IndexError):
                    return False
        else:
            self.fail("Independent terminal proof requires Darwin or Linux")

        while time.monotonic() < end:
            if terminal():
                self.assertLess(time.monotonic(), end,
                                "Terminal proof exceeded the existing barrier deadline")
                return
            # Only the fresh exact kernel observation proves success. This
            # bounded backoff is not an EOF/elapsed-time completion assumption.
            select.select([], [], [], max(0, min(0.01, end - time.monotonic())))
        self.fail("Exact owned payload did not become terminal before its barrier deadline")

    def test_capture_batch_finishes_after_captured_nested_owner_is_normally_reaped(self):
        for mode in ("esrch", "eperm", "success"):
            with self.subTest(native_observation=mode):
                previous_env = self.env
                process_start, gate_start = len(self.processes), len(self.gates)
                self.env = {**self.env, "GODOT_COORDINATOR_STATE_DIR":
                            str(self.tmp / f"reaped-owner-{mode}-state")}
                try:
                    keeper, keeper_gate = self.start(
                        "capture", self.project(f"reaped-keeper-{mode}"),
                        f"reaped-keeper-{mode}")
                    keeper_identity = self.admitted(keeper_gate, keeper)
                    batch_gate, control = self.gate(), self.gate()
                    host, port = control.listener.getsockname()
                    purpose = f"reaped-batch-{mode}"
                    nested_purpose = f"reaped-nested-{mode}"
                    argv = self.command("capture", None, purpose, batch_gate.command())[2:]
                    batch = subprocess.Popen(
                        [sys.executable, "-u", "-c", REAPED_NESTED_OWNER_COMMAND,
                         str(COORDINATOR.parent), mode, nested_purpose,
                         host, str(port), control.nonce, *argv],
                        env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, start_new_session=True)
                    self.processes.append(batch)
                    batch_identity = self.admitted(batch_gate, batch)
                    project = self.project(f"reaped-project-{mode}")
                    native_gate = self.gate()
                    nested = batch_gate.spawn(self.command(
                        "shared", project, nested_purpose, native_gate.command()))
                    self.admitted(native_gate, nested)
                    captured = self.admitted(control, batch)
                    self.assertEqual(captured["event"], "captured")
                    self.assertEqual(captured["owner"]["pid"], nested.pid)
                    # The outer transaction is already committed/unlocked.
                    # The real Gate parent now completes and reaps this nested
                    # supervisor before the outer monitor's next snapshot.
                    native_gate.release()
                    self.assertEqual(nested.wait(timeout=WAIT), 0)
                    native_gate.expect_closed(timeout=WAIT)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(nested.pid, 0)

                    successor, successor_gate = self.start(
                        "capture", project, f"reaped-successor-{mode}")
                    self.wait_record("waiting", f"reaped-successor-{mode}")
                    self.not_admitted(successor_gate)
                    batch_gate.release()
                    batch_gate.expect_closed(timeout=WAIT)
                    # EOF can precede interpreter exit. Keep the actual monitor
                    # paused until its exact unreaped payload is kernel-terminal,
                    # so a live launcher cannot mask an incorrectly retired veto.
                    self.wait_owned_payload_terminal(
                        batch_identity, batch.pid, captured["until"])
                    control.connection.settimeout(
                        max(0.001, captured["until"] - time.monotonic()))
                    self.assertEqual(control.request({"op": "resume"}), {"event": "resumed"})
                    if mode != "esrch":
                        continued = json.loads(control.reader.readline())
                        self.assertEqual(continued["event"], "unknown-continuation")
                        # An unknown native response must not complete the
                        # batch/free its capture slot, even though the actual
                        # payload and nested coordinator have both finished.
                        self.assertIsNone(batch.poll(), "Unknown evidence released the batch")
                        snapshot = self.status()
                        self.assertIn(purpose, [item["purpose"] for item in snapshot["active"]])
                        self.assertIn(f"reaped-successor-{mode}",
                                      [item["purpose"] for item in snapshot["waiting"]])
                        self.not_admitted(successor_gate)
                        control.connection.settimeout(
                            max(0.001, continued["until"] - time.monotonic()))
                        self.assertEqual(control.request({"op": "resume"}), {"event": "resumed"})

                    self.assertEqual(batch.wait(timeout=WAIT), 0,
                                     "Normally reaped nested owner left a sticky capture veto")
                    self.admitted(successor_gate, successor)
                    self.assertIsNone(keeper.poll())
                    self.assertEqual(keeper_gate.request({"op": "ping"})["pid"],
                                     keeper_identity["pid"])
                    self.finish(successor, successor_gate)
                    self.finish(keeper, keeper_gate)
                    self.assert_idle()
                finally:
                    # Each row has its own isolated domain and bounded actual
                    # actor cleanup, including RED failures. A release message
                    # opens either barrier and disables any later barrier.
                    case_gates = self.gates[gate_start:]
                    case_processes = self.processes[process_start:]
                    try:
                        for gate in case_gates:
                            gate.release()
                            if gate.connection is None:
                                gate.listener.close()
                        for process in case_processes:
                            try:
                                process.communicate(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.communicate(timeout=2)
                    finally:
                        for gate in case_gates:
                            gate.close()
                        del self.processes[process_start:]
                        del self.gates[gate_start:]
                        self.env = previous_env

    def test_nested_cancellation_during_cleanup_preserves_timeout_and_reaps_owned_child(self):
        survivor, survivor_gate = self.start(
            "shared", self.project("cleanup-survivor"), "cleanup-survivor")
        survivor_identity = self.admitted(survivor_gate, survivor)
        batch, batch_gate = self.start("capture", None, "cleanup-parent-batch")
        batch_identity = self.admitted(batch_gate, batch)
        project = self.project("nested-cleanup")
        native_gate = self.gate()
        cleanup_gate = self.gate()
        host, port = cleanup_gate.listener.getsockname()
        argv = self.command(
            "shared", project, "nested-cleanup-timeout",
            native_gate.command(notify_term=True), timeout=2)[2:]
        nested = batch_gate.spawn([
            sys.executable, "-u", "-c", CLEANUP_GATE_COMMAND, str(COORDINATOR.parent),
            host, str(port), cleanup_gate.nonce, *argv])
        native_identity = self.admitted(native_gate, nested)
        cleanup_identity = self.admitted(cleanup_gate, nested)
        # The fixture holds the original atomic-cleanup flock after TERM.
        # Bound both real receipts by that gate's original-deadline cap;
        # no scheduler-dependent leaf TERM grace is required.
        receipt_end = cleanup_identity["until"]
        native_gate.connection.settimeout(max(0.001, receipt_end - time.monotonic()))
        cleanup_gate.connection.settimeout(max(0.001, receipt_end - time.monotonic()))
        self.assertEqual(json.loads(native_gate.reader.readline()), {"event": "term"})
        os.kill(nested.pid, signal.SIGTERM)
        self.assertEqual(json.loads(cleanup_gate.reader.readline()),
                         {"event": "cancelled", "signal": signal.SIGTERM})
        self.assertEqual(cleanup_gate.request({"op": "resume"}), {"event": "resumed"})
        native_gate.connection.settimeout(WAIT)
        self.assertEqual(nested.wait(timeout=3.5), 124, "Cleanup cancellation replaced the timeout")
        native_gate.expect_closed()
        with self.assertRaises(ProcessLookupError, msg="Nested cleanup did not reap its owned child"):
            os.kill(native_identity["pid"], 0)
        self.assertIsNone(batch.poll())
        self.assertEqual(batch_gate.request({"op": "ping"})["pid"], batch_identity["pid"])
        self.assertIsNone(survivor.poll())
        self.assertEqual(survivor_gate.request({"op": "ping"})["pid"], survivor_identity["pid"])
        successor_gate = self.gate()
        successor = batch_gate.spawn(self.command(
            "shared", project, "nested-cleanup-successor", successor_gate.command()))
        self.admitted(successor_gate, successor)
        self.finish(successor, successor_gate)
        self.finish(batch, batch_gate)
        self.finish(survivor, survivor_gate)
        self.assert_idle()

    def test_external_root_cancellation_drains_nested_stubborn_child_before_releasing_claims(self):
        survivor, survivor_gate = self.start(
            "shared", self.project("root-cancel-survivor"), "root-cancel-survivor")
        survivor_identity = self.admitted(survivor_gate, survivor)
        started = time.monotonic()
        root, root_gate = self.start("capture", None, "external-cancel-root", timeout=2)
        root_identity = self.admitted(root_gate, root)
        project = self.project("root-cancel-nested")
        native_gate = self.gate()
        nested = root_gate.spawn(self.command(
            "shared", project, "external-cancel-nested",
            native_gate.command(ignore_term=True), timeout=2))
        native_identity = self.admitted(native_gate, nested)
        snapshot = self.status()
        self.assertEqual({record["purpose"] for record in snapshot["active"]},
                         {"root-cancel-survivor", "external-cancel-root", "external-cancel-nested"})
        root_record = next(record for record in snapshot["active"]
                           if record["purpose"] == "external-cancel-root")
        self.assertGreater(root_record["deadline"] - time.monotonic(), 0.3,
                           "Root must still be doing normal work, not deadline cleanup")
        root.send_signal(signal.SIGTERM)
        code = root.wait(timeout=3.5)
        stderr = ""
        if code != 143 and select.select([root.stderr], [], [], 0)[0]:
            stderr = os.read(root.stderr.fileno(), 8192).decode(errors="replace")
        self.assertEqual(code, 143, stderr)
        self.assertLess(time.monotonic() - started, 3.5)
        root_gate.expect_closed()
        native_gate.expect_closed()
        for identity in (root_identity, native_identity):
            with self.subTest(reaped_pid=identity["pid"]):
                with self.assertRaises(ProcessLookupError, msg="Owned nested child was abandoned"):
                    os.kill(identity["pid"], 0)
        self.assertIsNone(survivor.poll())
        self.assertEqual(survivor_gate.request({"op": "ping"})["pid"], survivor_identity["pid"])
        snapshot = self.status()
        self.assertEqual({record["purpose"] for record in snapshot["active"]},
                         {"root-cancel-survivor"})
        self.assertEqual(snapshot["waiting"], [])
        successor, successor_gate = self.start("capture", project, "after-root-cancel")
        self.admitted(successor_gate, successor)
        self.finish(successor, successor_gate)
        self.finish(survivor, survivor_gate)
        self.assert_idle()

    def test_forged_and_replayed_tokens_never_bypass_exclusive_owner(self):
        owner, owner_gate = self.start("exclusive", None, "exclusive-owner")
        identity = self.admitted(owner_gate, owner)
        self.assertTrue(identity["token"])
        queued = []
        for purpose, token in (("forged-token", "forged-test-token"),
                               ("replayed-token", identity["token"])):
            with self.subTest(credential=purpose):
                untrusted, untrusted_gate = self.start(
                    "shared", self.project(purpose), purpose,
                    extra_env={"GODOT_COORDINATOR_TOKEN": token})
                deadline = time.monotonic() + WAIT
                while untrusted.poll() is None:
                    snapshot = self.status()
                    self.not_admitted(untrusted_gate)
                    if any(record["purpose"] == purpose for record in snapshot["waiting"]):
                        break
                    self.assertLess(time.monotonic(), deadline,
                                    "Untrusted client neither rejected nor queued")
                    time.sleep(0.01)
                self.not_admitted(untrusted_gate)
                if untrusted.poll() is not None:
                    self.assertNotEqual(untrusted.returncode, 0)
                else:
                    queued.append((untrusted, untrusted_gate))
        self.finish(owner, owner_gate)
        for untrusted, untrusted_gate in queued:
            self.admitted(untrusted_gate, untrusted, diagnostics=True)
            self.finish(untrusted, untrusted_gate)
        self.assert_idle()

    def test_parent_death_preserves_surviving_child_project_and_capture_slot(self):
        project = self.project("survivor")
        owner, survivor_gate = self.start("capture", project, "dead-parent")
        self.admitted(survivor_gate, owner)
        owner.kill()  # Kill only this test's coordinator, not its still-live command.
        owner.wait(timeout=WAIT)
        second, second_gate = self.start("capture", self.project("second"), "second-capture")
        self.admitted(second_gate, second)
        third, third_gate = self.start("capture", self.project("third"), "third-capture")
        self.wait_record("waiting", "third-capture")
        same, same_gate = self.start("shared", project, "same-surviving-project")
        self.wait_record("waiting", "same-surviving-project")
        self.not_admitted(third_gate)
        self.not_admitted(same_gate)
        survivor_gate.release()
        survivor_gate.expect_closed(timeout=WAIT)
        self.admitted(third_gate, third)
        self.admitted(same_gate, same)
        self.finish(third, third_gate)
        self.finish(same, same_gate)
        self.finish(second, second_gate)
        self.assert_idle()

    def test_registered_detached_child_retains_claims_after_monitor_and_cli_death(self):
        project = self.project("registered-project")
        owner, monitor_gate = self.start("capture", project, "registered-owner")
        self.admitted(monitor_gate, owner)
        attached_gate = self.gate()
        attached = monitor_gate.spawn(attached_gate.command(), new_session=True)
        attached_identity = self.admitted(attached_gate, attached)
        registration = monitor_gate.spawn([
            sys.executable, str(COORDINATOR), "register", "--pid",
            str(attached_identity["pid"]), "--binary", attached_identity["binary"]], capture_stderr=True)
        self.assertEqual(registration.wait(timeout=WAIT), 0, registration.stderr)
        monitor_gate.release()
        monitor_gate.expect_closed(timeout=WAIT)
        owner.kill()
        owner.wait(timeout=WAIT)

        second, second_gate = self.start("capture", self.project("second"), "registered-second")
        self.admitted(second_gate, second)
        third, third_gate = self.start("capture", self.project("third"), "registered-third")
        self.wait_record("waiting", "registered-third")
        same, same_gate = self.start("shared", project, "registered-same-project")
        self.wait_record("waiting", "registered-same-project")
        self.not_admitted(third_gate)
        self.not_admitted(same_gate)

        attached_gate.release()
        attached_gate.expect_closed(timeout=WAIT)
        self.admitted(third_gate, third)
        self.admitted(same_gate, same)
        self.finish(third, third_gate)
        self.finish(same, same_gate)
        self.finish(second, second_gate)
        self.assert_idle()

    def test_numeric_registration_rejects_live_foreign_process_born_after_lease(self):
        project = self.project("foreign-registration")
        owner, monitor_gate = self.start("shared", project, "registration-owner")
        self.admitted(monitor_gate, owner)
        foreign_gate = self.gate()
        foreign = subprocess.Popen(
            foreign_gate.command(), env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(foreign)
        foreign_identity = self.admitted(foreign_gate, foreign)
        denied = monitor_gate.spawn([
            sys.executable, str(COORDINATOR), "register", "--pid",
            str(foreign_identity["pid"]), "--binary", foreign_identity["binary"]],
            capture_stderr=True)
        self.assertNotEqual(denied.wait(timeout=WAIT), 0, denied.stderr)
        self.assertEqual(foreign_gate.request({"op": "ping"})["pid"], foreign_identity["pid"])
        self.assertIsNone(owner.poll(), "Rejected registration disturbed the lease owner")
        self.assertEqual(self.wait_record("active", "registration-owner")["project"],
                         str(project.resolve()))
        self.finish(foreign, foreign_gate)
        self.finish(owner, monitor_gate)
        self.assert_idle()

    def test_forged_pending_receipt_becomes_stale_without_releasing_claims(self):
        project = self.project("unwitnessed-project")
        owner, monitor_gate = self.start("capture", project, "unwitnessed-owner")
        self.admitted(monitor_gate, owner)
        foreign_gate = self.gate()
        foreign = subprocess.Popen(
            foreign_gate.command(), env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(foreign)
        identity = self.admitted(foreign_gate, foreign)
        pending_dir = self.tmp / "pending-launch"
        pending_dir.mkdir(mode=0o700)
        pid_file = pending_dir / "native.json"
        pending = monitor_gate.spawn([
            sys.executable, str(COORDINATOR), "register", "--pid-file", str(pid_file),
            "--binary", identity["binary"]], capture_stderr=True)
        self.assertEqual(pending.wait(timeout=WAIT), 0, pending.stderr)
        launch = json.loads(Path(str(pid_file) + ".launch.json").read_text())
        # Even a receipt with the real capability and current kernel identity
        # cannot substitute for a witness from the target's actual Popen owner.
        receipt = {"api": 1, "pid": identity["pid"], "start": identity["start"],
                   "binary": identity["binary"], "launchcapability": launch["capability"]}
        pid_file.write_text(json.dumps(receipt) + "\n")
        pid_file.chmod(0o600)
        monitor_gate.release()
        monitor_gate.expect_closed(timeout=WAIT)
        owner.kill()
        owner.wait(timeout=WAIT)

        second, second_gate = self.start("capture", self.project("second"), "pending-second")
        self.admitted(second_gate, second)
        third, third_gate = self.start("capture", self.project("third"), "pending-third")
        self.wait_record("waiting", "pending-third")
        same, same_gate = self.start("shared", project, "pending-same-project")
        self.wait_record("waiting", "pending-same-project")
        self.not_admitted(third_gate)
        self.not_admitted(same_gate)
        self.assertEqual(foreign_gate.request({"op": "ping"})["pid"], identity["pid"])

        self.finish(foreign, foreign_gate)
        # The same on-disk receipt is now stale. Neither the forged live receipt
        # nor the stale receipt may clear an unresolved launch after owner death.
        self.wait_record("active", "unwitnessed-owner")
        self.wait_record("waiting", "pending-third")
        self.wait_record("waiting", "pending-same-project")
        self.not_admitted(third_gate)
        self.not_admitted(same_gate)
        for waiter in (third, same):
            waiter.terminate()
            waiter.wait(timeout=WAIT)
        self.finish(second, second_gate)
        # This deliberate fail-closed claim exists only in this owned temporary
        # state directory, removed by tearDown; no live fixture is abandoned.

    def test_authenticated_nested_capture_reuses_ancestor_capture_slot(self):
        outer, outer_gate = self.start("capture", None, "outer-batch")
        identity = self.admitted(outer_gate, outer)
        self.assertTrue(identity["token"], "Coordinator did not pass an authenticated lease")
        other, other_gate = self.start("capture", self.project("other"), "other-batch")
        self.admitted(other_gate, other)
        nested_gate = self.gate()
        nested = outer_gate.spawn(self.command("capture", self.project("nested"),
                                                "nested-batch", nested_gate.command()))
        self.admitted(nested_gate, nested)
        self.assertIsNone(other.poll())
        self.assertIsNone(outer.poll())
        self.finish(nested, nested_gate)
        self.finish(other, other_gate)
        self.finish(outer, outer_gate)
        self.assert_idle()

    def test_existing_batch_descendant_drains_through_exclusive_waiter(self):
        batch, batch_gate = self.start("capture", None, "draining-batch")
        self.admitted(batch_gate, batch)
        project = self.project("contended-leaf-project")
        exclusive, exclusive_gate = self.start("exclusive", project, "drain-exclusive")
        self.wait_record("waiting", "drain-exclusive")
        unrelated, unrelated_gate = self.start("shared", project, "drain-unrelated")
        self.wait_record("waiting", "drain-unrelated")
        leaf_gate = self.gate()
        leaf = batch_gate.spawn(self.command("shared", project, "draining-leaf", leaf_gate.command()))
        self.admitted(leaf_gate, leaf)
        self.wait_record("waiting", "drain-exclusive")
        self.wait_record("waiting", "drain-unrelated")
        self.not_admitted(exclusive_gate)
        self.not_admitted(unrelated_gate)
        self.finish(leaf, leaf_gate)
        self.finish(batch, batch_gate)
        self.admitted(exclusive_gate, exclusive)
        self.wait_record("waiting", "drain-unrelated")
        self.not_admitted(unrelated_gate)
        self.finish(exclusive, exclusive_gate)
        self.admitted(unrelated_gate, unrelated)
        self.finish(unrelated, unrelated_gate)
        self.assert_idle()

    def test_capture_capacity_waiter_yields_to_drain_then_keeps_eligible_project_fifo(self):
        first, first_gate = self.start("capture", None, "capacity-batch-A")
        self.admitted(first_gate, first)
        second, second_gate = self.start("capture", None, "capacity-batch-B")
        self.admitted(second_gate, second)
        project = self.project("capacity-contended-project")
        standalone, standalone_gate = self.start("capture", project, "capacity-root-C")
        self.wait_record("waiting", "capacity-root-C")

        def resume_standalone():
            if standalone.poll() is None:
                standalone.send_signal(signal.SIGCONT)

        self.addCleanup(resume_standalone)
        locker, lock_gate, _ = self.hold_state_lock()
        standalone.send_signal(signal.SIGSTOP)
        stopped_by = time.monotonic() + WAIT
        while True:
            pid, status = os.waitpid(standalone.pid, os.WUNTRACED | os.WNOHANG)
            if pid:
                self.assertTrue(os.WIFSTOPPED(status), "Owned capture waiter exited instead of stopping")
                break
            self.assertLess(time.monotonic(), stopped_by, "Owned capture waiter did not stop")
            select.select([], [], [], 0.01)
        # The transaction lock prevents stopping C inside a critical section.
        # Pausing only this owned client makes the later FIFO check independent
        # of whether C or B happens to poll the newly available slot first.
        self.finish(locker, lock_gate)

        first_leaf_gate = self.gate()
        first_leaf = first_gate.spawn(self.command(
            "shared", project, "capacity-leaf-A", first_leaf_gate.command()))
        self.admitted(first_leaf_gate, first_leaf)
        self.wait_record("waiting", "capacity-root-C")
        self.not_admitted(standalone_gate)
        self.finish(first_leaf, first_leaf_gate)
        self.finish(first, first_gate)

        eligible = self.wait_record("waiting", "capacity-root-C")
        self.assertEqual(eligible["blockers"], [], "C should now be eligible for the released slot")
        second_leaf_gate = self.gate()
        second_leaf = second_gate.spawn(self.command(
            "shared", project, "capacity-leaf-B", second_leaf_gate.command()))
        self.wait_record("waiting", "capacity-leaf-B")
        self.not_admitted(second_leaf_gate)
        resume_standalone()
        self.admitted(standalone_gate, standalone)
        self.wait_record("waiting", "capacity-leaf-B")
        self.not_admitted(second_leaf_gate)
        self.finish(standalone, standalone_gate)
        self.admitted(second_leaf_gate, second_leaf)
        self.finish(second_leaf, second_leaf_gate)
        self.finish(second, second_gate)
        self.assert_idle()

    def test_nested_siblings_cannot_share_one_project_claim(self):
        outer, outer_gate = self.start("capture", None, "sibling-batch")
        self.admitted(outer_gate, outer)
        project = self.project("sibling-project")
        first_gate, second_gate = self.gate(), self.gate()
        first = outer_gate.spawn(self.command("shared", project, "nested-first", first_gate.command()))
        self.admitted(first_gate, first)
        second = outer_gate.spawn(self.command("shared", project, "nested-second", second_gate.command()))
        self.wait_record("waiting", "nested-second")
        self.not_admitted(second_gate)
        self.finish(first, first_gate)
        self.admitted(second_gate, second)
        self.finish(second, second_gate)
        self.finish(outer, outer_gate)
        self.assert_idle()

    def test_nested_exclusive_upgrade_fails_instead_of_deadlocking(self):
        outer, outer_gate = self.start("shared", None, "shared-ancestor")
        self.admitted(outer_gate, outer)
        nested_gate = self.gate()
        nested = outer_gate.spawn(self.command("exclusive", self.project("upgrade"),
                                                "exclusive-upgrade", nested_gate.command(), timeout=15))
        self.assertNotEqual(nested.wait(timeout=3), 0)
        self.not_admitted(nested_gate)
        self.assertIsNone(outer.poll())
        self.finish(outer, outer_gate)
        self.assert_idle()


if __name__ == "__main__":
    unittest.main()
