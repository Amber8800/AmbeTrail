"""Optional reproducible capture benchmark; reports measurements without asserting unsupported claims."""
import argparse
import json
import os
import pathlib
import random
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dev_snapshot.common import settings_from
from dev_snapshot.git import Git
from dev_snapshot.snapshots import SnapshotService
from dev_snapshot.store import Store


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


parser = argparse.ArgumentParser()
parser.add_argument("--files", type=int, default=10000)
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
        start = time.perf_counter()
        core.capture()
        initial = time.perf_counter() - start
        for i in range(10):
            (root / f"source-{i:05}.bin").write_bytes(generator.randbytes(10240))
        start = time.perf_counter()
        core.capture("automatic", full=False, changed_paths={f"source-{i:05}.bin" for i in range(10)})
        incremental = time.perf_counter() - start
        print(json.dumps({"platform": sys.platform, "python": sys.version.split()[0],
                          "files": args.files, "rawMiB": args.files * 10240 / 1048576,
                          "initialSeconds": round(initial, 3), "incremental10FilesSeconds": round(incremental, 3),
                          "pythonPeakWorkingSetMiB": peak_memory_mib(), "storedMiB": store.usage() / 1048576,
                          "targets": {"initialSeconds": 10, "incrementalSeconds": 2, "additionalMemoryMiB": 200}}, indent=2))
    finally:
        store.close()
