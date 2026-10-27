#!/usr/bin/env python3
"""Atomic, per-user Godot admission; no daemon and no foreign-process signals.

CLI: run --mode shared|capture|exclusive [--project ROOT] -- COMMAND ...
Python: with lease(mode='capture', purpose='batch') as batch: ... batch.env
Project claims are deliberately non-reentrant. Batch owners omit project; each
engine/import leaf claims the canonical cache root. See --help for the API.
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Callable

VERSION = "1.0.0"
API_VERSION = 1
POLL_SECONDS = 0.05
TOKEN_ENV = "GODOT_COORDINATOR_TOKEN"
_LOCAL_LEASES = contextvars.ContextVar("godot_coordinator_leases", default=())
_BOOT_ID = None


class CoordinatorError(RuntimeError):
    exit_code = 2


class LeaseTimeout(CoordinatorError):
    exit_code = 124


class CoordinatorCancelled(CoordinatorError):
    exit_code = 130


def _check_request(deadline=None, cancel=None):
    if cancel is not None and cancel():
        raise CoordinatorCancelled("Coordinator admission was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise LeaseTimeout("Deadline expired during coordinator admission")


def _capture(command, deadline=None, cancel=None, timeout=2, check=False):
    """Bound discovery subprocesses; signal only our actual unreaped Popen."""
    end = time.monotonic() + timeout
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            _check_request(deadline, cancel)
            if time.monotonic() >= end:
                raise subprocess.TimeoutExpired(command, timeout)
            remaining = min(POLL_SECONDS, end - time.monotonic())
            if deadline is not None:
                remaining = min(remaining, max(0, deadline - time.monotonic()))
            try:
                stdout, stderr = process.communicate(timeout=max(0, remaining))
                if check and process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.returncode is None:
            process.kill()
            process.communicate()


def _boot_identity():
    global _BOOT_ID
    if _BOOT_ID is None:
        if sys.platform.startswith("linux"):
            _BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        elif sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            value = ctypes.create_string_buffer(128)
            size = ctypes.c_size_t(len(value))
            if libc.sysctlbyname(b"kern.bootsessionuuid", value, ctypes.byref(size), None, 0):
                raise CoordinatorError("Cannot establish Darwin boot identity")
            _BOOT_ID = os.fsdecode(value.value)
        if not _BOOT_ID:
            raise CoordinatorError("Cannot establish operating-system boot identity")
    return _BOOT_ID


def _identity(process):
    return {"pid": process["pid"], "start": process["start"]}


_DARWIN_LIB = None
_DARWIN_ARGMAX = None


def _darwin_argmax():
    global _DARWIN_ARGMAX
    if _DARWIN_ARGMAX is None:
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 2)(1, 8)  # KERN_ARGMAX
        value = ctypes.c_int()
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if libc.sysctl(mib, 2, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot query Darwin KERN_ARGMAX")
        if size.value != ctypes.sizeof(value) or value.value <= 0:
            raise OSError("Darwin KERN_ARGMAX returned an invalid size")
        _DARWIN_ARGMAX = value.value
    return _DARWIN_ARGMAX


class _BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "rfu")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        "nfiles", "pgid", "pjobc", "e_tdev", "e_tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64)]


class _VinfoStat(ctypes.Structure):
    # Complete Darwin SDK vinfo_stat; nested layout places vip_path at byte 152.
    _fields_ = [("vst_dev", ctypes.c_uint32), ("vst_mode", ctypes.c_uint16),
                ("vst_nlink", ctypes.c_uint16), ("vst_ino", ctypes.c_uint64),
                ("vst_uid", ctypes.c_uint32), ("vst_gid", ctypes.c_uint32)]
    _fields_ += [(name, ctypes.c_int64) for name in (
        "vst_atime", "vst_atimensec", "vst_mtime", "vst_mtimensec",
        "vst_ctime", "vst_ctimensec", "vst_birthtime", "vst_birthtimensec",
        "vst_size", "vst_blocks")]
    _fields_ += [("vst_blksize", ctypes.c_int32), ("vst_flags", ctypes.c_uint32),
                ("vst_gen", ctypes.c_uint32), ("vst_rdev", ctypes.c_uint32),
                ("vst_qspare", ctypes.c_int64 * 2)]


class _FSID(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]


class _VnodeInfo(ctypes.Structure):
    _fields_ = [("vi_stat", _VinfoStat), ("vi_type", ctypes.c_int),
                ("vi_pad", ctypes.c_int), ("vi_fsid", _FSID)]


class _VnodeInfoPath(ctypes.Structure):
    _fields_ = [("vip_vi", _VnodeInfo), ("vip_path", ctypes.c_char * 1024)]


class _ProcVnodePathInfo(ctypes.Structure):
    _fields_ = [("pvi_cdir", _VnodeInfoPath), ("pvi_rdir", _VnodeInfoPath)]


def _darwin_lib():
    global _DARWIN_LIB
    if _DARWIN_LIB is None:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                        ctypes.c_void_p, ctypes.c_int]
        library.proc_pidinfo.restype = ctypes.c_int
        library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        library.proc_pidpath.restype = ctypes.c_int
        library.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                         ctypes.c_void_p, ctypes.c_int]
        library.proc_listpids.restype = ctypes.c_int
        _DARWIN_LIB = library
    return _DARWIN_LIB


class _Processes:
    """One process-identity snapshot. Birth identity, never PID alone, is authority."""

    def __init__(self, deadline=None, cancel=None):
        self.deadline = deadline
        self.cancel = cancel
        self.items = {}
        self.zombies = {}
        self._args = {}
        self._env = {}
        self._exe = {}
        self.inspection_errors = {}
        if sys.platform.startswith("linux"):
            try:
                names = os.listdir("/proc")
            except OSError as exc:
                raise CoordinatorError(f"Cannot inspect process identities: {exc}") from exc
            for name in names:
                _check_request(deadline, cancel)
                if not name.isdecimal():
                    continue
                try:
                    text = Path("/proc", name, "stat").read_text()
                    fields = text[text.rfind(")") + 2:].split()
                    zombie = fields[0] in {"Z", "X"}
                    pid = int(name)
                    destination = self.zombies if zombie else self.items
                    destination[pid] = {"pid": pid, "ppid": int(fields[1]),
                                       "pgid": int(fields[2]), "sid": int(fields[3]),
                                       "start": "linux:" + fields[19],
                                       "name": text[text.find("(") + 1:text.rfind(")")]}
                except (OSError, ValueError, IndexError):
                    continue  # Exited during the snapshot.
        elif sys.platform == "darwin":
            try:
                result = _capture(["/bin/ps", "-axo", "pid="], deadline, cancel, check=True)
                library = _darwin_lib()
                for value in result.stdout.split():
                    _check_request(deadline, cancel)
                    pid = int(value)
                    info = _BSDInfo()
                    if library.proc_pidinfo(pid, 3, 0, ctypes.byref(info),
                                            ctypes.sizeof(info)) != ctypes.sizeof(info):
                        continue
                    destination = self.zombies if info.status == 5 else self.items
                    destination[pid] = {"pid": pid, "ppid": info.ppid,
                                       "pgid": info.pgid,
                                       "start": f"darwin:{info.start_sec}:{info.start_usec}",
                                       "born": info.start_sec + info.start_usec / 1000000,
                                       "name": os.fsdecode(bytes(info.comm).split(b"\0")[0])}
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise CoordinatorError(f"Cannot inspect Darwin process identities: {exc}") from exc
        else:
            raise CoordinatorError("Godot coordination requires Linux /proc or Darwin libproc")

    def matches(self, identity):
        process = self.items.get(identity.get("pid")) if identity else None
        return bool(process and process["start"] == identity.get("start"))

    def group_anchor_matches(self, identity, pgid):
        """Recheck an established anchor after observing group candidates.

        A matching birth cannot have exited/recycled between its established
        ownership and this observation. This is membership proof, not signal
        authority, and never promotes a bare inherited token.
        """
        try:
            if sys.platform == "darwin":
                info = _BSDInfo()
                if _darwin_lib().proc_pidinfo(identity["pid"], 3, 0, ctypes.byref(info),
                                             ctypes.sizeof(info)) != ctypes.sizeof(info):
                    return False
                start = f"darwin:{info.start_sec}:{info.start_usec}"
                return start == identity["start"] and info.pgid == pgid
            text = Path("/proc", str(identity["pid"]), "stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            return "linux:" + fields[19] == identity["start"] and int(fields[2]) == pgid
        except (OSError, ValueError, IndexError):
            return False

    def descendant(self, pid, roots):
        seen = set()
        while pid in self.items and pid not in seen:
            seen.add(pid)
            process = self.items[pid]
            if any(process["pid"] == root["pid"] and process["start"] == root["start"]
                   for root in roots):
                return True
            pid = process["ppid"]
        return False

    def executable(self, pid):
        if pid not in self._exe:
            try:
                if sys.platform == "darwin":
                    buffer = ctypes.create_string_buffer(4096)
                    if _darwin_lib().proc_pidpath(pid, buffer, len(buffer)) <= 0:
                        raise OSError("proc_pidpath unavailable")
                    value = os.fsdecode(buffer.value)
                else:
                    value = os.readlink(f"/proc/{pid}/exe")
                self._exe[pid] = str(Path(value).resolve())
            except OSError:
                self._exe[pid] = None
        return self._exe[pid]

    def arguments(self, pid):
        if pid not in self._args:
            try:
                if sys.platform == "darwin":
                    libc = ctypes.CDLL(None, use_errno=True)
                    mib = (ctypes.c_int * 3)(1, 49, pid)  # KERN_PROCARGS2
                    # XNU rejects oldlen > KERN_ARGMAX with EINVAL, even when
                    # the supplied allocation is larger than the actual argv.
                    size = ctypes.c_size_t(_darwin_argmax())
                    buffer = ctypes.create_string_buffer(size.value)
                    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
                        raise OSError(ctypes.get_errno(), "KERN_PROCARGS2 unavailable")
                    raw = buffer.raw[:size.value]
                    argc = int.from_bytes(raw[:4], sys.byteorder)
                    position = raw.index(b"\0", 4) + 1
                    while position < len(raw) and raw[position] == 0:
                        position += 1
                    values = raw[position:].split(b"\0")
                    self._args[pid] = [os.fsdecode(value) for value in values[:argc]]
                    self._env[pid] = dict(os.fsdecode(value).split("=", 1)
                                          for value in values[argc:] if b"=" in value)
                else:
                    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                    self._args[pid] = [os.fsdecode(value) for value in raw.split(b"\0") if value]
            except (OSError, ValueError) as exc:
                self._args[pid] = None
                self.inspection_errors[pid] = f"Cannot inspect native argument vector: {exc}"
        return self._args[pid]

    def environment(self, pid):
        if pid not in self._env:
            if sys.platform == "darwin":
                self.arguments(pid)
            else:
                try:
                    values = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                    self._env[pid] = dict(os.fsdecode(value).split("=", 1)
                                          for value in values if b"=" in value)
                except OSError:
                    self._env[pid] = None
        return self._env.get(pid)

    def cwd(self, pid):
        try:
            if sys.platform != "darwin":
                return Path(os.readlink(f"/proc/{pid}/cwd"))
            expected = self.items.get(pid)
            if expected is None:
                return None
            if (ctypes.sizeof(_VinfoStat), ctypes.sizeof(_VnodeInfo),
                    ctypes.sizeof(_VnodeInfoPath), ctypes.sizeof(_ProcVnodePathInfo),
                    _VnodeInfoPath.vip_path.offset, _ProcVnodePathInfo.pvi_rdir.offset
                    ) != (136, 152, 1176, 2352, 152, 1176):
                return None
            library = _darwin_lib()

            def same_birth():
                _check_request(self.deadline, self.cancel)
                birth = _BSDInfo()
                return (library.proc_pidinfo(pid, 3, 0, ctypes.byref(birth),
                                             ctypes.sizeof(birth)) == ctypes.sizeof(birth)
                        and birth.pid == pid
                        and f"darwin:{birth.start_sec}:{birth.start_usec}" == expected.get("start"))

            if not same_birth():
                return None
            info = _ProcVnodePathInfo()
            if library.proc_pidinfo(pid, 9, 0, ctypes.byref(info),
                                    ctypes.sizeof(info)) != ctypes.sizeof(info):
                return None
            raw = bytes(info.pvi_cdir.vip_path)
            # XNU forces the final byte to NUL, which cannot prove a full-length
            # path was not truncated. No usable current vnode means unknown.
            if not raw.startswith(b"/") or len(raw) >= 1023:
                return None
            path = Path(os.fsdecode(raw))
            named = path.stat()
            vnode = info.pvi_cdir.vip_vi.vi_stat
            if (not stat.S_ISDIR(named.st_mode) or not stat.S_ISDIR(vnode.vst_mode)
                    or (named.st_dev & 0xffffffff) != vnode.vst_dev
                    or named.st_ino != vnode.vst_ino or not same_birth()):
                return None
            return path
        except (OSError, ValueError):
            return None

    def born_after(self, pid, record):
        process = self.items[pid]
        if "born" in process:
            return process["born"] >= record["created_wall"]
        return int(process["start"].split(":")[1]) >= record["created_ticks"]


def _is_engine(binary):
    if not binary:
        return False
    name = Path(binary).name.lower()
    return (name in {"godot", "godot4", "godot3"}
            or name.startswith(("godot_", "godot.", "godot-")))


def _canonical(project):
    if project is None:
        return None, None
    try:
        root = Path(project).expanduser().resolve(strict=True)
        info = root.stat()
    except OSError as exc:
        raise CoordinatorError(f"Cannot resolve project {project}: {exc}") from exc
    if not root.is_dir():
        raise CoordinatorError(f"Project root is not a directory: {root}")
    return str(root), [info.st_dev, info.st_ino]


def _engine_project(processes, pid):
    arguments = processes.arguments(pid)
    if not arguments:
        return None
    project = None
    positional = None
    native_arguments = []
    for argument in arguments[1:]:
        if argument in {"--", "++"}:
            break
        native_arguments.append(argument)
    if any(argument in {"-u", "--upwards"} for argument in native_arguments):
        return None  # Existing metadata alone does not prove upward load behavior.
    native_options = iter(native_arguments)
    for argument in native_options:
        if argument == "--path":
            if project is not None:
                return None
            project = next(native_options, None)
            if not project or project.startswith("-") or project == "++":
                return None
        elif argument.startswith("--path="):
            # The native parser does not support this spelling.
            return None
        elif argument == "--gdscript-docs":
            # This native selector can choose a root independently of --path/cwd.
            return None
        elif argument.startswith("-") and argument.endswith("project.godot"):
            # Native suffix dispatch also accepts a leading-dash project file.
            return None
        elif not argument.startswith("-") and Path(argument).name == "project.godot":
            # Other native options may consume this token as an operand.
            if len(native_arguments) != 1:
                return None
            positional = argument
    if project is not None and positional is not None:
        return None
    cwd = processes.cwd(pid)
    if cwd is None or not cwd.is_absolute():
        return None
    if project is not None:
        if not Path(project).is_absolute():
            return None  # No independent proof that relative parsing has finished.
        candidate = Path(project)
    elif positional is not None:
        path = Path(positional)
        if not path.is_absolute() or not path.is_file():
            return None
        candidate = path.parent
    else:
        candidate = cwd
    try:
        root = _canonical(candidate)[0]
        # Observe the native's actual directory, not an unconsumed option or
        # pre-parser relative-path assumption. Do not invent an ancestor root.
        if _canonical(cwd)[0] != root or not (Path(root) / "project.godot").is_file():
            return None
        return root
    except CoordinatorError:
        return None


def _secure_directory(path):
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise CoordinatorError(f"Cannot open coordinator state directory {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CoordinatorError(f"Coordinator state must be a real directory, not a symlink: {path}")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CoordinatorError(f"Coordinator state must be owned by uid {os.getuid()} and mode 0700: {path}")


def _secure_open(path, flags):
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            os.close(fd)
            raise CoordinatorError(f"Unsafe coordinator state file ownership or permissions: {path}")
        return fd
    except OSError as exc:
        raise CoordinatorError(f"Cannot safely open coordinator state file {path}: {exc}") from exc


class _State:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        # A machine namespace cannot depend on caller HOME/XDG configuration.
        default = Path("/var/tmp") / f"godot-coordinator-{os.getuid()}"
        requested = Path(env.get("GODOT_COORDINATOR_STATE_DIR", str(default))).expanduser()
        if requested.is_symlink():
            raise CoordinatorError(f"Coordinator state cannot be a symlink: {requested}")
        self.path = requested.parent.resolve() / requested.name
        _secure_directory(self.path)
        self.domain = "production" if self.path == default.resolve() else "isolated"
        self.boot_id = _boot_identity()

    @contextlib.contextmanager
    def transaction(self, deadline=None, cancel=None, *, refresh=True):
        fd = _secure_open(self.path / "lock", os.O_CREAT | os.O_RDWR)
        try:
            while True:
                _check_request(deadline, cancel)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(POLL_SECONDS if deadline is None else
                               max(0, min(POLL_SECONDS, deadline - time.monotonic())))
            _check_request(deadline, cancel)
            data_path = self.path / "state.json"
            try:
                data_fd = _secure_open(data_path, os.O_RDONLY)
            except CoordinatorError:
                if data_path.exists() or data_path.is_symlink():
                    raise
                data = {"api": API_VERSION, "sequence": 0, "records": [],
                        "domain": self.domain, "boot_id": self.boot_id}
            else:
                with os.fdopen(data_fd) as stream:
                    try:
                        data = json.load(stream)
                    except (ValueError, OSError) as exc:
                        raise CoordinatorError(f"Corrupt coordinator state {data_path}; do not delete live claims: {exc}") from exc
                if data.get("api") != API_VERSION or not isinstance(data.get("records"), list):
                    raise CoordinatorError(f"Unsupported coordinator state API in {data_path}")
                if data.get("domain") != self.domain:
                    raise CoordinatorError(f"State domain mismatch at {self.path}; preserve inherited coordinator environment")
                if not data.get("boot_id"):
                    raise CoordinatorError(f"State has no boot identity; refuse unbound live claims at {self.path}")
                if data["boot_id"] != self.boot_id:
                    data = {"api": API_VERSION, "sequence": 0, "records": [],
                            "domain": self.domain, "boot_id": self.boot_id}
            markers = []
            for action in ("cancel", "release"):
                for marker_path in self.path.glob(action + "-*.json"):
                    marker = _read_private_json(marker_path)
                    record = next((item for item in data["records"] if item["id"] == marker.get("id")), None)
                    if record is None:
                        markers.append(marker_path)
                        continue
                    if (marker.get("owner") != record["owner"]
                            or not isinstance(marker.get("token"), str)
                            or not secrets.compare_digest(marker["token"], record["token"])):
                        raise CoordinatorError("Coordinator cancellation/release marker is not authenticated")
                    if action == "release":
                        record["released"] = True
                    elif (record["status"] == "waiting" or
                          (not record.get("child") and not record.get("members")
                           and not record.get("attached") and not record.get("pending"))):
                        data["records"].remove(record)
                    else:
                        raise CoordinatorError("Cancellation marker cannot discard a running reservation")
                    markers.append(marker_path)
            processes = _Processes(deadline, cancel) if refresh else None
            if processes is not None:
                _refresh(data, processes)
            _check_request(deadline, cancel)
            yield data, processes
            _check_request(deadline, cancel)
            temporary = self.path / (".state-" + secrets.token_hex(12))
            temporary_fd = _secure_open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                with os.fdopen(temporary_fd, "w") as stream:
                    json.dump(data, stream, separators=(",", ":"), allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, data_path)
                _sync_directory(self.path)
                # Never erase a cancellation witness before its state update
                # is durable: a crash must not resurrect a live-host waiter.
                for marker_path in markers:
                    try:
                        marker_path.unlink()
                    except FileNotFoundError:
                        pass
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        finally:
            os.close(fd)


def _indirect_identity(record, processes, pid, binary, domain):
    process = processes.items.get(pid)
    if process is None or not processes.born_after(pid, record):
        raise CoordinatorError("Indirect PID must be live and created after its lease began")
    actual = processes.executable(pid)
    if actual != binary:
        raise CoordinatorError(f"Indirect PID {pid} executable mismatch: expected {binary!r}, actual {actual!r}; --binary must name the kernel executable, not a launcher")
    if domain == "production":
        project = _engine_project(processes, pid)
        if not record["project"] or project != record["project"]:
            raise CoordinatorError(f"Indirect engine project is unknown or mismatched: PID {pid}, "
                             f"project {project!r}, expected {record['project']!r}")
    return {**_identity(process), "binary": binary}


def _pending_path(path):
    requested = Path(path).expanduser()
    parent = requested.parent.resolve(strict=True)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CoordinatorError(f"Pending native PID file requires an owned mode-0700 parent: {parent}")
    return parent / requested.name, [info.st_dev, info.st_ino]


def _read_private_json(path):
    fd = _secure_open(path, os.O_RDONLY)
    with os.fdopen(fd) as stream:
        try:
            value = json.load(stream)
        except ValueError as exc:
            raise CoordinatorError("Invalid private launch JSON") from exc
    if not isinstance(value, dict) or value.get("api") != API_VERSION:
        raise CoordinatorError("Unsupported private launch witness API")
    return value


_EXCLUSIVE_RENAME = None


def _rename_exclusive(source, destination):
    global _EXCLUSIVE_RENAME
    if _EXCLUSIVE_RENAME is None:
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            if sys.platform == "darwin":
                operation = libc.renamex_np
                operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            else:
                operation = libc.renameat2
                operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                      ctypes.c_char_p, ctypes.c_uint]
            operation.restype = ctypes.c_int
        except AttributeError as exc:
            raise CoordinatorError("Platform lacks atomic no-replace rename; private publication refused") from exc
        _EXCLUSIVE_RENAME = operation
    if sys.platform == "darwin":
        result = _EXCLUSIVE_RENAME(os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
    else:
        result = _EXCLUSIVE_RENAME(-100, os.fsencode(source), -100,
                                   os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(destination))


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private_json(path, value):
    path = Path(path)
    payload = memoryview(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())
    temporary = path.parent / (".publish-" + secrets.token_hex(16))
    fd = _secure_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise OSError(errno.EIO, "Private JSON publication made no progress")
            written += count
        os.fsync(fd)
        os.close(fd)
        fd = None
        _rename_exclusive(temporary, path)
        _sync_directory(path.parent)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _capability_matches(launch, capability):
    return (isinstance(capability, str) and secrets.compare_digest(
        launch["capability_sha256"], hashlib.sha256(capability.encode()).hexdigest()))


def _read_pending(pending):
    path, key = _pending_path(pending["path"])
    if key != pending["directory_key"]:
        raise CoordinatorError("Pending launch private directory identity changed")
    value = _read_private_json(path)
    if (not isinstance(value.get("pid"), int) or value["pid"] <= 0
            or not isinstance(value.get("start"), str)
            or value.get("binary") != pending["binary"]
            or not _capability_matches(pending, value.get("launchcapability"))):
        raise CoordinatorError("Pending native witness identity or launch capability is invalid")
    return value


def _resolve_pending(record, processes):
    retained = []
    for pending in record.get("pending", []):
        try:
            if not Path(pending["path"]).exists() and not Path(pending["path"]).is_symlink():
                pending["reason"] = "awaiting owned native birth/PID file; launch outcome unresolved"
                retained.append(pending)
                continue
            receipt = _read_pending(pending)
            identity = pending.get("identity")
            if identity is None or any(receipt[key] != identity[key] for key in ("pid", "start", "binary")):
                raise CoordinatorError("Pending native file has no matching centrally authorized birth witness")
            if processes.matches(identity):
                if identity not in record["attached"]:
                    record["attached"].append(identity)
            else:
                for launch in record.get("launches", []):
                    if launch["path"] == pending["path"]:
                        launch["completed"] = True
        except (CoordinatorError, OSError) as exc:
            pending["reason"] = str(exc)
            retained.append(pending)
    record["pending"] = retained


def _refresh(data, processes):
    retained = []
    for record in data["records"]:
        if record["status"] == "waiting":
            if (processes.matches(record["owner"]) and
                    (record.get("deadline") is None or record["deadline"] > time.monotonic())):
                retained.append(record)
            continue
        roots = [identity for identity in record.get("members", []) if processes.matches(identity)]
        record.pop("orphans", None)
        child = record.get("child")
        if child and processes.matches(child):
            roots.append(child)
        if child:
            # Session/group continuity needs a previously established birth.
            # A still-live leader or known member can anchor the group even
            # after the coordinator dies. No anchors => no PGID/token adoption.
            anchors = [*record.get("members", []), child]
            candidates = [_identity(process) for process in processes.items.values()
                          if process["pgid"] == child["pid"]]
            if candidates and any(processes.group_anchor_matches(anchor, child["pid"])
                                  for anchor in anchors):
                roots.extend(candidates)
        if record["kind"] == "context":
            # Independent thread contexts may share one Python owner. Only
            # children receiving this particular context's env belong to it.
            for pid, process in processes.items.items():
                if pid == record["owner"]["pid"]:
                    continue
                if processes.descendant(pid, [record["owner"]]):
                    environment = processes.environment(pid)
                    if environment and environment.get(TOKEN_ENV) == record["token"]:
                        roots.append(_identity(process))
        members = []
        for pid, process in processes.items.items():
            if processes.descendant(pid, roots):
                if record["kind"] != "context" or pid != record["owner"]["pid"]:
                    members.append(_identity(process))
        record["members"] = members
        record["attached"] = [identity for identity in record.get("attached", [])
                              if processes.matches(identity)]
        _resolve_pending(record, processes)
        if ((processes.matches(record["owner"]) and not record.get("released"))
                or members or record["attached"] or record.get("pending")):
            retained.append(record)
    data["records"] = retained


def _authenticated(data, processes, env):
    token = env.get(TOKEN_ENV)
    if not token:
        return None
    for record in data["records"]:
        if record["status"] != "active" or not secrets.compare_digest(record["token"], token):
            continue
        roots = ([record["owner"]] if record["kind"] == "context"
                 else ([record["child"]] if record.get("child") else []))
        roots += record.get("members", [])
        if processes.descendant(os.getpid(), roots):
            return record
    return None


def _ancestors(data, record):
    by_id = {item["id"]: item for item in data["records"]}
    chain = []
    while record and record not in chain:
        chain.append(record)
        record = by_id.get(record.get("parent"))
    return chain


def _foreign(data, processes, domain):
    if domain != "production":
        return []
    owned = []
    for record in data["records"]:
        if record["status"] == "active":
            owned += record.get("members", []) + record.get("attached", [])
            if record.get("child"):
                owned.append(record["child"])
    result = []
    producers = []
    # Legacy matrices retain batch intent between native engines. Match real
    # interpreter/script identities, not argv words. Preview servers are not
    # permanent capture batches.
    for pid, process in processes.items.items():
        _check_request(processes.deadline, processes.cancel)
        binary = processes.executable(pid)
        if not binary or Path(binary).name.lower() not in {"node", "nodejs"}:
            continue
        if processes.descendant(pid, owned):
            continue
        arguments = processes.arguments(pid)
        if not arguments or len(arguments) < 2 or arguments[1].startswith("-"):
            continue
        script = Path(arguments[1])
        if not script.is_absolute():
            cwd = processes.cwd(pid)
            if cwd is None:
                continue
            script = cwd / script
        script = script.resolve()
        if script.name not in {"capture_matrix.mjs", "godot-window.mjs"} or not script.is_file():
            continue
        identity = _identity(process)
        delegated = any(item["status"] == "active" and
                        (item["capture_root"] or item["exclusive_root"]) and
                        processes.descendant(item["owner"]["pid"], [identity])
                        for item in data["records"])
        requesting = any(item["status"] == "waiting" and
                         item["owner"]["pid"] == os.getpid() and
                         item["mode"] in {"capture", "exclusive"} and
                         processes.descendant(os.getpid(), [identity])
                         for item in data["records"])
        if delegated or requesting:
            continue
        project = _engine_project(processes, pid)
        if project is None and (script.parent.parent / "project.godot").is_file():
            project = _canonical(script.parent.parent)[0]
        producers.append({**_identity(process), "binary": binary, "project": project,
                          "kind": "capture-producer", "capture_batch": True,
                          "reason": f"legacy unmanaged capture producer {script}"})
    producer_roots = [item for item in producers if not processes.descendant(
        processes.items[item["pid"]]["ppid"], producers)]
    result.extend(producer_roots)
    for pid, process in processes.items.items():
        _check_request(processes.deadline, processes.cancel)
        binary = processes.executable(pid)
        possible_engine = _is_engine(binary) or (binary is None and _is_engine(process.get("name")))
        if not possible_engine or processes.descendant(pid, owned):
            continue
        project = _engine_project(processes, pid)
        arguments = processes.arguments(pid)
        headed = arguments is None or "--headless" not in arguments
        result.append({**_identity(process), "binary": binary, "project": project,
                       "kind": "engine",
                       "capture_batch": headed and not processes.descendant(pid, producer_roots),
                       "reason": ("unmanaged engine" if project else
                                  "unmanaged engine has unknown project ownership; " +
                                  processes.inspection_errors.get(pid, "no canonical --path or project-bearing cwd"))})
    return result


def _legacy(processes, domain):
    if domain != "production":
        return []
    # The old timing marker may predate a caller's isolated HOME/XDG settings.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    paths = {home / ".cache/chase3d/engine-exclusive"}
    if os.environ.get("XDG_CACHE_HOME"):
        paths.add(Path(os.environ["XDG_CACHE_HOME"]) / "chase3d/engine-exclusive")
    if os.environ.get("CHASE_ENGINE_LEASE"):
        paths.add(Path(os.environ["CHASE_ENGINE_LEASE"]))
    result = []
    for path in sorted(paths):
        if not path.exists():
            continue
        try:
            pid = int((path / "pid").read_text().strip())
        except (OSError, ValueError):
            result.append({"path": str(path), "pid": None,
                           "reason": "legacy timing lease is being acquired or has unreadable owner"})
            continue
        if pid not in processes.items:
            continue  # Never remove another implementation's files.
        arguments = processes.arguments(pid) or []
        actual_wrapper = any(Path(value).name == "engine_lease.sh" and index + 1 < len(arguments)
                             and arguments[index + 1] == "exclusive"
                             for index, value in enumerate(arguments))
        if actual_wrapper and processes.descendant(os.getpid(), [_identity(processes.items[pid])]):
            continue
        result.append({"path": str(path), **_identity(processes.items[pid]),
                       "reason": "live legacy timing-exclusive owner"})
    return result


def _same_project(first, second):
    return bool(first.get("project") and second.get("project") and
                (first["project"] == second["project"] or first.get("project_key") == second.get("project_key")))


def _nonproject_blockers(data, record, foreign, legacy):
    """The same machine prerequisites govern admission and project priority."""
    blockers = [{"kind": "legacy", **item} for item in legacy]
    for item in foreign:
        unknown_engine = item.get("kind") == "engine" and not item["project"]
        unknown_capture = item.get("kind") == "capture-producer" and not item["project"]
        if (unknown_engine or record["mode"] == "exclusive"
                or (unknown_capture and record["mode"] == "capture")):
            blockers.append({"kind": "foreign", **item})

    active = [item for item in data["records"]
              if item["status"] == "active" and item["id"] != record["id"]]
    inherited = record["machine_root"] != record["id"]
    if inherited and not any(item["machine_root"] == record["machine_root"] for item in active):
        blockers.append({"kind": "reservation",
                         "reason": "authenticated ancestor machine reservation is no longer active"})
    for item in active:
        exclusive_other_family = (item["exclusive_root"]
                                  and item["machine_root"] != record["machine_root"])
        if exclusive_other_family or (not inherited and record["mode"] == "exclusive"):
            blockers.append({"kind": "owner", "id": item["id"], "pid": item["owner"]["pid"],
                             "project": item["project"], "purpose": item["purpose"], "mode": item["mode"]})

    earlier = [item for item in data["records"] if item["status"] == "waiting"
               and item["sequence"] < record["sequence"]]
    if not inherited:
        for item in earlier:
            if item["mode"] == "exclusive":
                blockers.append({"kind": "waiter", "id": item["id"],
                                 "purpose": item["purpose"], "mode": item["mode"]})
    if record["capture_root"] == record["id"]:
        captures = {item["capture_root"] for item in active if item["capture_root"]}
        foreign_captures = sum(bool(item.get("capture_batch")) for item in foreign)
        if len(captures) + foreign_captures >= 2:
            blockers.append({"kind": "capture", "reason": "both capture batch slots are occupied"})
        elif any(item["capture_root"] == item["id"] for item in earlier):
            blockers.append({"kind": "capture-waiter", "reason": "earlier capture batch is queued"})
    return blockers


def _blockers(data, record, foreign, legacy):
    blockers = _nonproject_blockers(data, record, foreign, legacy)
    for item in foreign:
        if item["project"] and item["project"] == record["project"]:
            blocker = {"kind": "foreign", **item}
            if blocker not in blockers:
                blockers.append(blocker)
    for item in data["records"]:
        if item["status"] == "active" and item["id"] != record["id"] and _same_project(item, record):
            blocker = {"kind": "owner", "id": item["id"], "pid": item["owner"]["pid"],
                       "project": item["project"], "purpose": item["purpose"], "mode": item["mode"]}
            if blocker not in blockers:
                blockers.append(blocker)

    draining = record["machine_root"] != record["id"]
    for item in data["records"]:
        if (item["status"] != "waiting" or item["sequence"] >= record["sequence"]
                or not _same_project(item, record)):
            continue
        # An admitted family must be able to drain past a waiter which cannot
        # obtain its machine resources until that family releases them. Reuse
        # actual admission rules, not mode-specific exceptions. Once eligible,
        # the earlier waiter retains project priority, even if not scheduled.
        if draining and _nonproject_blockers(data, item, foreign, legacy):
            continue
        blocker = {"kind": "waiter", "id": item["id"],
                   "purpose": item["purpose"], "mode": item["mode"]}
        if blocker not in blockers:
            blockers.append(blocker)
    return blockers


def _deadline(timeout, env):
    deadline = None
    if timeout is not None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise CoordinatorError("timeout must be a positive finite number of seconds")
        deadline = time.monotonic() + timeout
    inherited = env.get("GODOT_COORDINATOR_DEADLINE")
    if inherited:
        try:
            value = float(inherited)
            if not math.isfinite(value):
                raise ValueError
            deadline = value if deadline is None else min(value, deadline)
        except ValueError as exc:
            raise CoordinatorError("Invalid inherited GODOT_COORDINATOR_DEADLINE") from exc
    return deadline


class Lease:
    def __init__(self, env, identifier, state, deadline, owner):
        self.env = env
        self.id = identifier
        self._state = state
        self._deadline = deadline
        self._owner = owner

    @property
    def deadline(self):
        return self._deadline

    def remaining(self):
        if self._deadline is None:
            return math.inf
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise LeaseTimeout("Coordinator lease deadline expired before further work")
        return remaining

    def validate_command(self, argv):
        """Check command domain admission, not ownership or deadline authority."""
        if self._state.domain == "isolated" and _is_engine(str(Path(argv[0]).resolve())):
            raise CoordinatorError("Explicit nondefault state is an isolated synthetic-test domain, not authorized for real Godot engines; use the production default state")


def _mark_record(state, identifier, token, owner, action):
    # A cancelled thread in a still-live host must not wait on the contested
    # lock merely to remove its barrier. The next lock holder authenticates
    # this durable notice and commits its state update before deleting it.
    path = state.path / f"{action}-{identifier}.json"
    value = {"api": API_VERSION, "id": identifier, "token": token, "owner": owner}
    if path.exists() or path.is_symlink():
        if _read_private_json(path) != value:
            raise CoordinatorError("Existing coordinator lifecycle notice does not match its owner")
        return
    try:
        _write_private_json(path, value)
    except FileExistsError:
        if _read_private_json(path) != value:
            raise CoordinatorError("Concurrent coordinator lifecycle notice has a different owner")


def _acquire(mode, project, timeout, purpose, kind, cancel=None):
    if mode not in {"shared", "capture", "exclusive"}:
        raise CoordinatorError(f"Unknown lease mode {mode!r}")
    parents = _LOCAL_LEASES.get()
    env = dict(parents[-1].env if parents else os.environ)
    deadline = _deadline(timeout, env)
    state = _State(env)
    project, key = _canonical(project)
    identifier = secrets.token_hex(16)
    token = secrets.token_urlsafe(32)
    record = None
    try:
        with state.transaction(deadline, cancel) as (data, processes):
            owner = processes.items.get(os.getpid())
            if owner is None:
                raise CoordinatorError("Cannot establish coordinator caller process identity")
            inherited = _authenticated(data, processes, env)
            chain = _ancestors(data, inherited)
            for ancestor in chain:
                if ancestor.get("deadline") is not None:
                    deadline = (ancestor["deadline"] if deadline is None
                                else min(deadline, ancestor["deadline"]))
            if any(item.get("deadline") is not None and item["deadline"] <= time.monotonic() for item in chain):
                raise LeaseTimeout("Inherited coordinator lease deadline expired")
            exclusive = next((item["exclusive_root"] for item in chain if item["exclusive_root"]), None)
            if mode == "exclusive" and inherited and not exclusive:
                raise CoordinatorError("Cannot upgrade a shared/capture ancestor to exclusive; acquire exclusive at the outermost caller")
            if project and any(item["project"] == project or (key and item.get("project_key") == key) for item in chain):
                raise CoordinatorError(f"Project {project} is already owned by an ancestor; batch/controller leases must omit --project")
            capture = next((item["capture_root"] for item in chain if item["capture_root"]), None)
            data["sequence"] += 1
            record = {"id": identifier, "token": token, "sequence": data["sequence"],
                      "status": "waiting", "mode": mode, "purpose": str(purpose),
                      "project": project, "project_key": key, "owner": _identity(owner),
                      "child": None, "members": [], "attached": [], "pending": [], "kind": kind,
                      "created_wall": time.time(),
                      "created_ticks": (int(time.clock_gettime(time.CLOCK_BOOTTIME) * os.sysconf("SC_CLK_TCK"))
                                        if sys.platform.startswith("linux") else None),
                      "deadline": deadline,
                      "parent": inherited["id"] if inherited else None,
                      "machine_root": inherited["machine_root"] if inherited else identifier,
                      "exclusive_root": exclusive or (identifier if mode == "exclusive" else None),
                      "capture_root": capture or (identifier if mode == "capture" and not exclusive else None),
                      "blockers": []}
            data["records"].append(record)
        announced = False
        while True:
            if cancel is not None and cancel():
                raise CoordinatorCancelled(f"Cancelled while waiting for {purpose}")
            if deadline is not None and time.monotonic() >= deadline:
                raise LeaseTimeout(f"Deadline expired waiting for {purpose}: {record.get('blockers', [])}")
            with state.transaction(deadline, cancel) as (data, processes):
                record = next((item for item in data["records"] if item["id"] == identifier), None)
                if record is None:
                    raise CoordinatorError("Coordinator waiting record disappeared")
                # Internal machine blockers already prove denial. Avoid slow
                # discovery until a grant is otherwise possible; project FIFO
                # still needs the full foreign/legacy-aware calculation.
                record["blockers"] = _nonproject_blockers(data, record, [], [])
                if not record["blockers"]:
                    foreign = _foreign(data, processes, state.domain)
                    legacy = _legacy(processes, state.domain)
                    record["blockers"] = _blockers(data, record, foreign, legacy)
                _check_request(deadline, cancel)
                if not record["blockers"]:
                    record["status"] = "active"
                    env[TOKEN_ENV] = token
                    env["GODOT_COORDINATOR_STATE_DIR"] = str(state.path)
                    env["GODOT_COORDINATOR_DOMAIN"] = state.domain
                    env["GODOT_COORDINATOR"] = str(Path(env.get("GODOT_COORDINATOR", __file__)).expanduser().resolve())
                    if deadline is not None:
                        env["GODOT_COORDINATOR_DEADLINE"] = str(deadline)
                    admitted = Lease(env, identifier, state, deadline, record["owner"])
                else:
                    admitted = None
                if admitted is None and not announced:
                    print(f"godot-coordinator: waiting for {purpose}: "
                          + json.dumps(record["blockers"], separators=(",", ":")), file=sys.stderr)
                    announced = True
            if admitted is not None:
                _check_request(deadline, cancel)
                return admitted
            delay = POLL_SECONDS if deadline is None else min(POLL_SECONDS, max(0, deadline - time.monotonic()))
            time.sleep(delay)
    except BaseException:
        if record is not None:
            _mark_record(state, identifier, token, record["owner"], "cancel")
        raise


def _owned_members(data, identifier, processes):
    record = next((item for item in data["records"] if item["id"] == identifier), None)
    if record is None:
        return [], None
    return [identity for identity in record.get("members", [])
            if processes.matches(identity) and identity["pid"] != os.getpid()], record


def _capture_nested_owners(data, ownership, candidates):
    """Capture authenticated veto witnesses before any refresh can prune them."""
    own = next((record for record in data["records"] if record["id"] == ownership.id), None)
    if (own is None or own["owner"] != ownership._owner
            or ownership._owner["pid"] != os.getpid()
            or not secrets.compare_digest(own["token"], ownership.env[TOKEN_ENV])):
        raise CoordinatorError("Cannot verify owned cleanup reservation; KILL withheld")
    for record in data["records"]:
        if (record["id"] == ownership.id or record["kind"] != "command"
                or record["status"] != "active"):
            continue
        chain = _ancestors(data, record)
        if (any(ancestor["id"] == ownership.id for ancestor in chain)
                or (record["machine_root"] == own["machine_root"]
                    and chain[-1].get("parent"))):
            # A missing intermediate parent leaves retained family ancestry
            # unknown, never proven unrelated.
            owner = _identity(record["owner"])
            candidates[(owner["pid"], owner["start"])] = owner


def _known_process_absent(pid):
    """Prove no running lookup target, not a free PID, reap, or signal authority."""
    try:
        os.getpgid(pid)
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


def _nested_owners_protected(candidates, processes, pgid):
    discharged = []
    protected = False
    for key, candidate in candidates.items():
        owner = processes.items.get(candidate["pid"])
        if owner is None:
            # Snapshot omission alone is unknown. A separate read-only kernel
            # ESRCH can retire this saved birth; success/denial stays protected.
            protected = (candidate["pid"] not in processes.zombies
                         and not _known_process_absent(candidate["pid"]))
        else:
            protected = processes.matches(candidate) and owner["pgid"] == pgid
        if protected:
            break
        discharged.append(key)
    for key in discharged:
        del candidates[key]
    return protected


def _owned_child_terminal(process, identity):
    """Observe our exact unreaped direct child terminal, without poll/wait."""
    if (process.returncode is not None or not identity
            or identity.get("pid") != process.pid):
        return False
    try:
        if sys.platform == "darwin":
            info = _BSDInfo()
            if _darwin_lib().proc_pidinfo(process.pid, 3, 1, ctypes.byref(info),
                                         ctypes.sizeof(info)) != ctypes.sizeof(info):
                return False
            terminal = (info.pid == process.pid and info.ppid == os.getpid()
                        and info.status == 5
                        and f"darwin:{info.start_sec}:{info.start_usec}" == identity.get("start"))
        elif sys.platform.startswith("linux"):
            text = Path("/proc", str(process.pid), "stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            terminal = (int(text[:text.find("(")].strip()) == process.pid
                        and fields[0] == "Z" and int(fields[1]) == os.getpid()
                        and "linux:" + fields[19] == identity.get("start"))
        else:
            return False
        return terminal and process.returncode is None
    except (OSError, ValueError, IndexError):
        return False


def _darwin_owned_group_dead(process, identity):
    """Prove a reserved direct-child group is zombie-only, never grant signals."""
    if (sys.platform != "darwin" or process.returncode is not None or not identity
            or identity.get("pid") != process.pid):
        return False
    library = _darwin_lib()
    width = ctypes.sizeof(ctypes.c_int)

    def members():
        size = library.proc_listpids(2, process.pid, None, 0)  # PROC_PGRP_ONLY
        if size <= 0 or size % width:
            return None
        buffer = (ctypes.c_int * (size // width))()
        used = library.proc_listpids(2, process.pid, buffer, size)
        # A full buffer may be truncated. Every returned PID must be readable.
        if used <= 0 or used >= size or used % width:
            return None
        pids = list(buffer[:used // width])
        if any(pid <= 0 for pid in pids) or len(set(pids)) != len(pids):
            return None
        result = {}
        for pid in pids:
            info = _BSDInfo()
            # Nonzero PROC_PIDTBSDINFO argument includes stable zombies.
            if library.proc_pidinfo(pid, 3, 1, ctypes.byref(info),
                                    ctypes.sizeof(info)) != ctypes.sizeof(info):
                return None
            if info.pid != pid or info.pgid != process.pid or info.status != 5:
                return None
            result[pid] = (f"darwin:{info.start_sec}:{info.start_usec}", info.ppid)
        return result

    try:
        observed = members()
        # Re-enumerate after all members were observed dead: an exiting member
        # could have forked between the first enumeration and its status read.
        if (not observed or observed.get(process.pid) != (identity["start"], os.getpid())
                or members() != observed or process.returncode is not None):
            return False
        return True
    except OSError:
        return False  # An unreadable/unknown group is never proof of absence.


def _wait_dead_owned_group(process, identity, end):
    """Confirm Darwin's exiting group without signals or releasing its anchor."""
    if sys.platform != "darwin":
        return False
    while time.monotonic() < end:
        if _darwin_owned_group_dead(process, identity):
            return True
        time.sleep(max(0, min(POLL_SECONDS, end - time.monotonic())))
    return False


def _cleanup_owned(ownership, process, *, identity=None, candidates=None):
    """Signal only our session/group while its direct leader remains unreaped.

    Unlike PID snapshots, an unreaped direct Popen child reserves its PID. The
    child was created with start_new_session=True, so its group cannot be
    replaced by an unrelated group before we finish all group signals.
    """
    if process.returncode is not None:
        return
    cleanup_end = time.monotonic() + 0.3
    if ownership.deadline is not None:
        cleanup_end = min(cleanup_end, ownership.deadline)
    if identity is None and sys.platform == "darwin":
        # Registration may have timed out before recording a birth. Only our
        # still-unreaped actual child can supply this cleanup-local witness;
        # it is neither durable registration nor completion/signal authority.
        try:
            info = _BSDInfo()
            if (_darwin_lib().proc_pidinfo(process.pid, 3, 1, ctypes.byref(info),
                                           ctypes.sizeof(info)) == ctypes.sizeof(info)
                    and info.pid == process.pid and info.ppid == os.getpid()
                    and info.pgid == process.pid and info.start_sec > 0
                    and info.start_usec < 1000000 and process.returncode is None):
                identity = {"pid": process.pid,
                            "start": f"darwin:{info.start_sec}:{info.start_usec}"}
        except OSError:
            pass  # Unreadable is unknown, never permission to ignore a signal error.

    def reap_owned():
        # No PID/group signals are allowed after this reap.
        try:
            process.wait(timeout=min(0.05, max(0.0, cleanup_end - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass

    def signal_group(signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin returns EPERM for a zombie-only group. Unknown evidence
            # still fails closed; the actual unreaped Popen remains authority.
            if not _darwin_owned_group_dead(process, identity):
                raise

    try:
        signal_group(signal.SIGTERM)
    except PermissionError:
        # A Darwin exit can reject TERM while BSD status is still SRUN.
        # Keep strict proof and the original error; wait only outside the lock.
        if not _wait_dead_owned_group(process, identity, cleanup_end - 0.05):
            raise
        reap_owned()
        return
    # A nested supervisor must finish its separately-sessioned native before
    # this group can be killed. State is only a veto, never signal authority.
    # Keep the veto and KILL under one admission lock: another nested command
    # cannot acquire/register a child between the decision and the signal.
    # Unknown owner births survive every retry, not only one state refresh.
    candidates = {} if candidates is None else candidates
    until = max(time.monotonic(), cleanup_end - 0.05)
    while True:
        # Attempt the atomic veto immediately after TERM. An unprotected expired
        # leaf has no minimum TERM grace; the fixed tail is for proof and reap.
        signalling = False
        try:
            with ownership._state.transaction(cleanup_end, refresh=False) as (data, _):
                _capture_nested_owners(data, ownership, candidates)
                # No descendant witness means no admitted nested supervisor;
                # keep admission locked through KILL without a process scan.
                processes = None
                if candidates:
                    processes = _Processes(cleanup_end)
                    _refresh(data, processes)
                    if not processes.matches(ownership._owner):
                        raise CoordinatorError("Cannot verify owned cleanup birth; KILL withheld")
                protected = _nested_owners_protected(candidates, processes, process.pid)
                if not protected:
                    _check_request(cleanup_end)
                    signalling = True
                    signal_group(signal.SIGKILL)
                    signalling = False
            if not protected:
                break
        except (CoordinatorError, OSError) as exc:
            if signalling:
                # The admission transaction has unwound and released its
                # lock. Never wait for a transient exit under that lock.
                if (isinstance(exc, PermissionError)
                        and _wait_dead_owned_group(process, identity, cleanup_end - 0.05)):
                    break
                raise  # A real owned-group signal failure is not masked.
            print(f"godot-coordinator: owned cleanup incomplete; claims retained: {exc}",
                  file=sys.stderr)
            break
        if time.monotonic() >= until:
            print("godot-coordinator: owned nested supervisor still draining; claims retained",
                  file=sys.stderr)
            break
        time.sleep(max(0, min(POLL_SECONDS, until - time.monotonic())))
    reap_owned()


@contextlib.contextmanager
def lease(mode="shared", project=None, timeout=None, purpose="Godot work", cancel: Callable[[], bool] | None = None):
    """Acquire one atomic reservation; yield a Lease with the complete child env.

    Local nesting uses contextvars, never process-wide environment mutation.
    cancel is polled during admission only. Caller controls cancellation after
    yield. Python bodies are cooperative: use .remaining() for actual execution
    and owned-Popen cleanup budgets. No snapshot-derived PID is signaled by a
    context. Exceeded deadlines raise LeaseTimeout on exit; surviving verified
    children retain their reservations.
    """
    ownership = _acquire(mode, project, timeout, purpose, "context", cancel)
    stack_token = _LOCAL_LEASES.set((*_LOCAL_LEASES.get(), ownership))
    stopped = threading.Event()
    expired = threading.Event()

    def monitor():
        while not stopped.wait(POLL_SECONDS):
            if ownership._deadline is not None and time.monotonic() >= ownership._deadline:
                expired.set()
                return
            try:
                with ownership._state.transaction(cancel=stopped.is_set):
                    pass  # Preserve identities without signal authority.
            except CoordinatorCancelled:
                return

    worker = threading.Thread(target=monitor, name="godot-lease", daemon=True)
    worker.start()
    try:
        yield ownership
        if expired.is_set() or (ownership._deadline is not None and time.monotonic() >= ownership._deadline):
            raise LeaseTimeout(f"Deadline expired during {purpose}")
    finally:
        stopped.set()
        worker.join()
        _LOCAL_LEASES.reset(stack_token)
        _mark_record(ownership._state, ownership.id, ownership.env[TOKEN_ENV], ownership._owner, "release")


def _exec_child(fd, command):
    with os.fdopen(fd, "rb") as stream:
        payload = stream.read()
    if not payload:
        return 125
    try:
        overrides = json.loads(payload)
        environment = dict(os.environ)
        environment.update(overrides)
        inherited_deadline = environment.get("GODOT_COORDINATOR_DEADLINE")
        _check_request(float(inherited_deadline) - 0.3 if inherited_deadline else None)
        os.execvpe(command[0], command, environment)
    except LeaseTimeout as exc:
        print(f"godot-coordinator: timeout: {exc}", file=sys.stderr)
        return 124
    except (OSError, ValueError) as exc:
        print(f"godot-coordinator: cannot execute {command[0]}: {exc}", file=sys.stderr)
        return 127


def _run(args, cancel=None):
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise CoordinatorError("run requires -- COMMAND ...")
    child_env = {}
    for value in args.child_env:
        key, separator, entry = value.partition("=")
        if (not separator or not key or "\0" in value
                or key == "GODOT_COORDINATOR" or key.startswith("GODOT_COORDINATOR_")):
            raise CoordinatorError("Invalid --child-env or reserved coordinator environment key")
        child_env[key] = entry
    ownership = _acquire(args.mode, args.project, args.timeout, args.purpose, "command", cancel)
    work_end = ownership.deadline - 0.3 if ownership.deadline is not None else None
    process = None
    child_identity = None
    candidates = {}
    signals_finished = False
    read_fd = write_fd = None
    try:
        _check_request(work_end, cancel)
        ownership.validate_command(command)
        _check_request(work_end, cancel)
        read_fd, write_fd = os.pipe()
        # The helper waits for the durable identity record before exec. If its
        # coordinator dies in the spawn/write gap, EOF prevents an orphan engine.
        _check_request(work_end, cancel)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                    "_exec", str(read_fd), "--", *command],
                                   env=ownership.env, pass_fds=(read_fd,), start_new_session=True)
        os.close(read_fd)
        read_fd = None
        _check_request(work_end, cancel)
        with ownership._state.transaction(work_end, cancel) as (data, processes):
            record = next(item for item in data["records"] if item["id"] == ownership.id)
            child = processes.items.get(process.pid)
            if child is None:
                raise CoordinatorError("Owned command disappeared before admission handshake")
            child_identity = _identity(child)
            record["child"] = child_identity
            record["members"] = [child_identity]
        _check_request(work_end, cancel)
        payload = json.dumps(child_env, separators=(",", ":")).encode()
        while payload:
            _check_request(work_end, cancel)
            count = os.write(write_fd, payload)
            payload = payload[count:]
        _check_request(work_end, cancel)
        os.close(write_fd)
        write_fd = None
        if args.ready_fd is not None:
            payload = json.dumps({"pid": process.pid, "token": ownership.env[TOKEN_ENV]}).encode() + b"\n"
            os.write(args.ready_fd, payload)
            os.close(args.ready_fd)
        phase_end = work_end
        resolving_completion = False
        while True:
            try:
                _check_request(phase_end, cancel)
                with ownership._state.transaction(phase_end, cancel, refresh=False) as (data, _):
                    _capture_nested_owners(data, ownership, candidates)
                    processes = _Processes(phase_end, cancel)
                    _refresh(data, processes)
                    if candidates and not processes.matches(ownership._owner):
                        raise CoordinatorError("Cannot verify owned cleanup birth; KILL withheld")
                    protected = _nested_owners_protected(candidates, processes, process.pid)
                    members, record = _owned_members(data, ownership.id, processes)
                    attached = record.get("attached", []) if record else []
                    pending = record.get("pending", []) if record else []
                    complete = (not members and not attached and not protected
                                and _owned_child_terminal(process, child_identity))
                    if complete and pending:
                        raise CoordinatorError("Owned launcher exited with unresolved pending native launch; claim retained; inspect status --json")
                    if resolving_completion and not complete:
                        # Abort rather than commit pruning an unresolved raw
                        # witness before the ordinary cleanup veto sees it.
                        raise LeaseTimeout(f"Deadline expired during {args.purpose}; owned group cleanup attempted; surviving claims retained")
            except LeaseTimeout:
                # The work cutoff is not an exit status. Only a fresh exact
                # terminal child may spend the unchanged total tail resolving
                # completed work; never reap an anchor to discover this fact.
                if resolving_completion or not _owned_child_terminal(process, child_identity):
                    raise LeaseTimeout(f"Deadline expired during {args.purpose}; owned group cleanup attempted; surviving claims retained") from None
                resolving_completion = True
                phase_end = ownership.deadline
                continue
            if complete and _owned_child_terminal(process, child_identity):
                _check_request(ownership.deadline, cancel)
                remaining = (None if ownership.deadline is None else
                             max(0, ownership.deadline - time.monotonic()))
                # Completion is resolved. All possible group signals are now
                # finished, even if this final bounded wait cannot return.
                signals_finished = True
                try:
                    code = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired as exc:
                    raise LeaseTimeout("Deadline expired reaping completed owned command") from exc
                return code if code >= 0 else 128 - code
            if resolving_completion:
                raise LeaseTimeout(f"Deadline expired during {args.purpose}; owned group cleanup attempted; surviving claims retained")
            time.sleep(POLL_SECONDS if work_end is None else
                       max(0, min(POLL_SECONDS, work_end - time.monotonic())))
    except BaseException:
        if process is not None and not signals_finished:
            _cleanup_owned(ownership, process, identity=child_identity, candidates=candidates)
        raise
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        _mark_record(ownership._state, ownership.id, ownership.env[TOKEN_ENV], ownership._owner, "release")


def _public_record(record):
    hidden = {"token", "project_key", "members", "created_wall", "capability",
              "launchcapability", "capability_sha256"}

    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items() if key not in hidden}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return redact(record)


def _status(args):
    state = _State()
    with state.transaction() as (data, processes):
        foreign = _foreign(data, processes, state.domain)
        legacy = _legacy(processes, state.domain)
        for record in data["records"]:
            if record["status"] == "waiting":
                record["blockers"] = _blockers(data, record, foreign, legacy)
        result = {"api": API_VERSION, "version": VERSION, "domain": state.domain,
                  "state_dir": str(state.path), "boot_id": state.boot_id,
                  "active": [_public_record(item) for item in data["records"] if item["status"] == "active"],
                  "waiting": [_public_record(item) for item in data["records"] if item["status"] == "waiting"],
                  "foreign": foreign, "legacy": legacy}
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(f"Godot coordinator {VERSION} (API {API_VERSION}, {state.domain})")
        for group in ("active", "waiting", "foreign", "legacy"):
            for record in result[group]:
                print(group + ": " + json.dumps(record, separators=(",", ":")))
    return 0


def _check(args):
    if args.project is not None:
        print("godot-coordinator: project claims are never inherited; acquire a new leaf run --project", file=sys.stderr)
        return 1
    state = _State()
    with state.transaction() as (data, processes):
        record = _authenticated(data, processes, os.environ)
        if not record or (record.get("deadline") is not None and record["deadline"] <= time.monotonic()):
            return 1
        if args.mode == "exclusive":
            return 0 if record["exclusive_root"] else 1
        if args.mode == "capture":
            return 0 if record["capture_root"] or record["exclusive_root"] else 1
        return 0


def _register(args):
    state = _State()
    with state.transaction() as (data, processes):
        record = _authenticated(data, processes, os.environ)
        if record is None:
            raise CoordinatorError("Indirect PID registration requires authenticated lease ancestry")
        binary = str(Path(args.binary).resolve(strict=True))
        if args.pid_file:
            path, key = _pending_path(args.pid_file)
            if path.exists() or path.is_symlink():
                raise CoordinatorError("Register pending native launch before creating its private PID file")
            if any(item["path"] == str(path) for item in record.get("launches", [])):
                raise CoordinatorError("Native PID file was already registered")
            capability = secrets.token_urlsafe(32)
            pending = {"path": str(path), "directory_key": key, "binary": binary,
                       "domain": state.domain,
                       "capability_sha256": hashlib.sha256(capability.encode()).hexdigest(),
                       "reason": "awaiting centrally authorized native birth witness"}
            manifest = {"api": API_VERSION, "capability": capability, "project": record["project"],
                        "binary": binary, "state_dir": str(state.path),
                        "coordinator": str(Path(__file__).resolve())}
            _write_private_json(Path(str(path) + ".launch.json"), manifest)
            record["pending"].append(pending)
            record.setdefault("launches", []).append(dict(pending))
        else:
            identity = _indirect_identity(record, processes, args.pid, binary, state.domain)
            roots = ([record["child"]] if record.get("child") else record.get("members", []))
            if not processes.descendant(args.pid, roots):
                raise CoordinatorError("Numeric PID registration requires a genuine owned descendant in every domain")
            if identity not in record["attached"]:
                record["attached"].append(identity)
            _resolve_pending(record, processes)
    return 0


def _launch_context(args):
    path, key = _pending_path(args.pid_file)
    manifest = None
    if args.launch_file:
        manifest_path = Path(args.launch_file).resolve(strict=True)
        if manifest_path != Path(str(path) + ".launch.json"):
            raise CoordinatorError("Launch capability manifest must be bound to the registered private witness path")
        manifest = _read_private_json(manifest_path)
        if not isinstance(manifest.get("state_dir"), str):
            raise CoordinatorError("Launch manifest has no coordinator namespace")
        state = _State({"GODOT_COORDINATOR_STATE_DIR": manifest["state_dir"]})
    else:
        state = _State()
    return state, path, key, manifest


def _authorized_launch(data, processes, path, key, manifest):
    authenticated = _authenticated(data, processes, os.environ)
    candidates = data["records"] if manifest else _ancestors(data, authenticated)
    for record in candidates:
        launch = next((item for item in record.get("launches", [])
                       if item["path"] == str(path)), None)
        if launch is None:
            continue
        if launch["directory_key"] != key:
            raise CoordinatorError("Native launch private directory identity changed")
        if manifest and not _capability_matches(launch, manifest.get("capability")):
            raise CoordinatorError("Native launch capability is invalid")
        if manifest is None and authenticated is None:
            break
        return record, launch
    raise CoordinatorError("Native launch is not authorized by ancestry or its bound capability")


def _witness(args):
    state, path, key, manifest = _launch_context(args)
    if manifest is None:
        raise CoordinatorError("Owning wrapper witness requires its private --launch-file capability")
    with state.transaction() as (data, processes):
        record, launch = _authorized_launch(data, processes, path, key, manifest)
        if launch.get("completed"):
            raise CoordinatorError("A completed native launch capability cannot be reused")
        target = processes.items.get(args.pid)
        wrapper = processes.items.get(os.getppid())
        if target is None:
            raise CoordinatorError("Native exited before live executable/project identity was witnessed; claim retained")
        if wrapper is None or target["ppid"] != wrapper["pid"]:
            raise CoordinatorError("Witness PID must be an actual unreaped direct child of the calling wrapper")
        identity = _indirect_identity(record, processes, args.pid, launch["binary"], state.domain)
        if launch.get("identity") and launch["identity"] != identity:
            raise CoordinatorError("Native launch already has a different birth identity")
        receipt = {"api": API_VERSION, **identity, "launchcapability": manifest["capability"]}
        if path.exists() or path.is_symlink():
            existing = _read_pending(launch)
            if existing != receipt:
                raise CoordinatorError("Existing native witness differs; refusing stale/replaced receipt")
        else:
            _write_private_json(path, receipt)
        launch["identity"] = identity
        launch["wrapper"] = _identity(wrapper)
        if identity not in record["attached"]:
            record["attached"].append(identity)
        record["pending"] = [item for item in record["pending"] if item["path"] != str(path)]
    return 0


def _complete(args):
    state, path, key, manifest = _launch_context(args)
    with state.transaction() as (data, processes):
        record, launch = _authorized_launch(data, processes, path, key, manifest)
        if args.not_launched:
            if path.exists() or path.is_symlink() or launch.get("identity"):
                raise CoordinatorError("Cannot report not-launched after a native witness exists")
        else:
            identity = launch.get("identity")
            receipt = _read_pending(launch)
            if (identity is None or args.pid != identity["pid"]
                    or any(receipt[field] != identity[field] for field in ("pid", "start", "binary"))):
                raise CoordinatorError("Native completion lacks its centrally established birth witness")
            if args.pid in processes.items or args.pid in processes.zombies:
                raise CoordinatorError("Cannot complete a live, unreaped, unknown, or reused native PID")
        record["pending"] = [item for item in record.get("pending", []) if item["path"] != str(path)]
        launch["completed"] = True
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "API 1: shared=parallel distinct projects; capture=max two batches; exclusive=machine alone. "
        "Tokens require real process ancestry. Projects are non-reentrant. Explicit nondefault "
        "GODOT_COORDINATOR_STATE_DIR is for isolated synthetic tests only. Focus/headless/mute "
        "policy remains the caller's responsibility; no foreign process is terminated."))
    parser.add_argument("--version", action="version", version=f"godot-coordinator {VERSION} (API {API_VERSION})")
    commands = parser.add_subparsers(dest="operation", required=True)
    run = commands.add_parser("run", help="reserve resources for an owned command's entire lifetime")
    run.add_argument("--mode", choices=("shared", "capture", "exclusive"), required=True)
    run.add_argument("--project", help="canonical cache root; omit on batch/controller owners")
    run.add_argument("--timeout", type=float, help="total admission, execution and owned-cleanup seconds")
    run.add_argument("--purpose", default="Godot work")
    run.add_argument("--ready-fd", type=int, help="write one JSON {pid,token} after durable child registration")
    run.add_argument("--child-env", action="append", default=[], metavar="KEY=VALUE",
                     help="apply only to final command exec, never coordinator/helper interpreters")
    run.add_argument("command", nargs=argparse.REMAINDER)
    status = commands.add_parser("status", help="show owners, waiters, unmanaged engines and legacy blockers")
    status.add_argument("--json", action="store_true")
    check = commands.add_parser("check", help="check authenticated inherited machine context, never project reuse")
    check.add_argument("--mode", choices=("shared", "capture", "exclusive"), required=True)
    check.add_argument("--project")
    register = commands.add_parser("register", help="retain an authenticated launcher's indirect native engine identity")
    target = register.add_mutually_exclusive_group(required=True)
    target.add_argument("--pid", type=int)
    target.add_argument("--pid-file", help="register absent private native PID file before launching")
    register.add_argument("--binary", required=True)
    complete = commands.add_parser("complete", help="report trusted owned launch completion without releasing a live engine")
    complete.add_argument("--pid-file", required=True)
    complete.add_argument("--launch-file", help="bound private capability for a reparented owning wrapper")
    outcome = complete.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--pid", type=int)
    outcome.add_argument("--not-launched", action="store_true")
    witness = commands.add_parser("witness", help="bind an owning wrapper's unreaped direct-child birth identity")
    witness.add_argument("--pid", type=int, required=True)
    witness.add_argument("--pid-file", required=True)
    witness.add_argument("--launch-file", required=True)
    execute = commands.add_parser("_exec", help=argparse.SUPPRESS)
    execute.add_argument("fd", type=int)
    execute.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.operation == "_exec":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        return _exec_child(args.fd, command)

    received_signal = None

    def interrupted(signum, _frame):
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum

    def check_cancel():
        if received_signal is not None:
            error = CoordinatorCancelled(f"Interrupted by signal {received_signal}")
            error.exit_code = 128 + received_signal
            raise error

    prior = {}
    managed_signals = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    if args.operation == "run":
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, managed_signals)
        try:
            # Preserve the direct child's unreaped PID/group lifetime.
            prior[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            for signum in managed_signals:
                prior[signum] = signal.signal(signum, interrupted)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    try:
        if args.operation == "run":
            return _run(args, cancel=check_cancel)
        return {"status": _status, "check": _check, "register": _register,
                "complete": _complete, "witness": _witness}[args.operation](args)
    except CoordinatorError as exc:
        prefix = "godot-coordinator: timeout: " if isinstance(exc, LeaseTimeout) else "godot-coordinator: "
        print(prefix + str(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(f"godot-coordinator: {exc}", file=sys.stderr)
        return 2
    finally:
        # Nonraising handlers stay installed through cleanup and error selection.
        # SDK imports do not install handlers, and other CLI operations retain
        # their normal interruptible signal behavior.
        if prior:
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, managed_signals)
            try:
                for signum, handler in prior.items():
                    signal.signal(signum, handler)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)


if __name__ == "__main__":
    raise SystemExit(main())
