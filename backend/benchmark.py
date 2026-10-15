"""Optional reproducible capture benchmark; reports measurements without asserting unsupported claims."""
import argparse
import functools
import json
import os
import pathlib
import random
import sys
import tempfile
import time
import threading
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dev_snapshot.common import settings_from
from dev_snapshot.git import Git
from dev_snapshot.snapshots import SnapshotService
from dev_snapshot.store import Store
from dev_snapshot.engine import Session
from dev_snapshot import common, snapshots, store as store_module


def peak_memory_mib():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        get_memory = ctypes.WinDLL("psapi").GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        ok = get_memory(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return counters.PeakWorkingSetSize / 1048576 if ok else None
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1048576 if sys.platform == "darwin" else 1024)


def process_io():
    """Process transfer counters, including cached I/O; not physical disk traffic or Git children."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "readOperations", "writeOperations", "otherOperations", "readBytes", "writeBytes", "otherBytes")]
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters)]
    kernel.GetProcessIoCounters.restype = wintypes.BOOL
    counters = Counters()
    if not kernel.GetProcessIoCounters(kernel.GetCurrentProcess(), ctypes.byref(counters)):
        return None
    return {name: getattr(counters, name) for name, _ in counters._fields_}


class Profile:
    """Inclusive call times summed across both workers; rows must not be added together."""
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def wrap(self, name, function):
        @functools.wraps(function)
        def measured(*args, **kwargs):
            start = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                with self.lock:
                    row = self.rows.setdefault(name, {"calls": 0, "workerSeconds": 0.0})
                    row["calls"] += 1
                    row["workerSeconds"] += elapsed
        return measured

    def install(self, stack):
        for owner, name in (
            (Git, "run"), (Git, "files"), (Git, "context"), (snapshots.Policy, "exclusion"),
            (SnapshotService, "read_entry"), (SnapshotService, "head_entries"),
            (Store, "put"), (Store, "get"), (Store, "save_snapshot"),
            (snapshots, "safe_path"), (store_module, "atomic_write"),
            (store_module.gzip, "compress"), (common.os, "fsync"),
        ):
            label = f"{owner.__name__}.{name}"
            stack.enter_context(patch.object(owner, name, self.wrap(label, getattr(owner, name))))


parser = argparse.ArgumentParser()
parser.add_argument("--files", type=int, default=10000)
parser.add_argument("--extended", action="store_true", help="Also measure reconciliation, bursts, repeated captures and idle ticks")
parser.add_argument("--profile", action="store_true", help="Collect inclusive stage timings in both read workers")
parser.add_argument("--output", type=pathlib.Path, help="Save JSON measurements as well as printing them")
args = parser.parse_args()
if not 10 <= args.files <= 10000:
    parser.error("--files must be between 10 and 10000")
with tempfile.TemporaryDirectory(prefix="dev-snapshot-benchmark-") as temporary:
    base = pathlib.Path(temporary)
    root = base / "repo"
    root.mkdir()
    git = Git(root)
    git.run("init", "-b", "main")
    generator = random.Random(42)
    for i in range(args.files):
        (root / f"source-{i:05}.bin").write_bytes(generator.randbytes(10240))
    identity = git.identity()
    store = Store(base / "storage", identity)
    try:
        core = SnapshotService(git, store, settings_from(None))
        measurements = {}
        profile = Profile()
        def measure(name, action, *, expected_snapshot=None):
            profile.rows = {}
            io_before = process_io()
            cpu = time.process_time()
            start = time.perf_counter()
            with ExitStack() as stack:
                if args.profile:
                    profile.install(stack)
                result = action()
            elapsed = time.perf_counter() - start
            cpu = time.process_time() - cpu
            io_after = process_io()
            measurements[name] = {"seconds": elapsed, "pythonCpuSeconds": cpu,
                                  "pythonCpuPercentOfOneCore": 100 * cpu / elapsed,
                                  "pythonIo": {key: io_after[key] - io_before[key] for key in io_before}
                                  if io_before is not None and io_after is not None else None,
                                  **({"inclusiveProfile": dict(profile.rows)} if args.profile else {})}
            print(f"{name}: {elapsed:.3f}s", file=sys.stderr, flush=True)
            if expected_snapshot is not None and (result is not None) != expected_snapshot:
                raise RuntimeError(f"Unexpected snapshot publication in {name}")
            return result
        measure("initial", core.capture, expected_snapshot=True)
        for i in range(10):
            (root / f"source-{i:05}.bin").write_bytes(generator.randbytes(10240))
        measure("incremental10Files", lambda: core.capture(
            "automatic", full=False, changed_paths={f"source-{i:05}.bin" for i in range(10)}), expected_snapshot=True)
        if args.extended:
            measure("metadataReconcile", lambda: core.capture("automatic", full=False), expected_snapshot=False)
            measure("fullHashReconcile", lambda: core.capture("automatic", full=True), expected_snapshot=False)
            burst = min(100, args.files)
            for i in range(burst):
                (root / f"source-{i:05}.bin").write_bytes(generator.randbytes(10240))
            measure(f"burst{burst}Files", lambda: core.capture(
                "automatic", full=False, changed_paths={f"source-{i:05}.bin" for i in range(burst)}), expected_snapshot=True)
            for iteration in range(5):
                for i in range(10):
                    (root / f"source-{i:05}.bin").write_bytes(generator.randbytes(10240))
                measure(f"repeatedIncremental{iteration + 1}", lambda: core.capture(
                    "automatic", full=False, changed_paths={f"source-{i:05}.bin" for i in range(10)}), expected_snapshot=True)
            session = Session(git, store, core.config)
            def idle():
                for _ in range(25):
                    if session.tick():
                        raise RuntimeError("Unexpected capture during the idle benchmark")
                    time.sleep(0.2)
            measure("idle5Seconds", idle)
            # Check every object referenced by the final manifest outside measured intervals.
            store.snapshot(core.latest()["id"], verify=True)
        result = {"platform": sys.platform, "python": sys.version.split()[0],
                          "files": args.files, "rawMiB": args.files * 10240 / 1048576,
                          "initialSeconds": round(measurements["initial"]["seconds"], 3),
                          "incremental10FilesSeconds": round(measurements["incremental10Files"]["seconds"], 3),
                          "pythonPeakWorkingSetMiB": peak_memory_mib(), "storedMiB": store.usage() / 1048576,
                          "measurements": measurements,
                          "notes": "Fresh isolated repo/store, seeded random untracked files; OS caches are not flushed. CPU/I/O exclude Git children and VS Code. I/O includes cached transfers. Profile times overlap and add worker durations. Peak memory covers setup and all scenarios.",
                          "targets": {"initialSeconds": 10, "incrementalSeconds": 2, "additionalMemoryMiB": 200}}
        output = json.dumps(result, indent=2)
        if args.output:
            args.output.write_text(output + "\n", encoding="utf-8")
        print(output)
    finally:
        store.close()
