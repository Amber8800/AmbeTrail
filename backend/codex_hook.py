"""Codex Stop command hook: no model, transcript, network, or continuation output."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dev_snapshot.agent_history import AgentHistory, MAX_INPUT, small_json
from dev_snapshot.common import atomic_write, encode, is_link, utc_now


def main():
    authorized = False
    try:
        if len(sys.argv) != 5 or sys.argv[1] != "--storage" or sys.argv[3] != "--grant":
            raise ValueError("Unsupported hook invocation")
        storage, grant = pathlib.Path(sys.argv[2]), sys.argv[4]
        if any(is_link(p) for p in (storage, storage.parent, storage.parent.parent)):
            raise ValueError("Invalid hook storage")
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(data) <= MAX_INPUT:
            policy = small_json(storage / "consent.json")
            # The approved identity is metadata, never a command or transcript path.
            if policy.get("enabled") is True and policy.get("grant") == grant:
                authorized = True
                history = AgentHistory(storage, {"id": policy["repositoryId"], "root": policy["root"],
                                                "gitDir": policy.get("gitDir")})
                collected = history.collect(json.loads(data), grant)
                if collected and (storage / "hook-status.json").exists():
                    atomic_write(storage / "hook-status.json", encode({"schema": 1, "state": "ok", "updatedAt": utc_now()}))
    except Exception:
        # Hook failure must not cause Codex to retry, resume, inject context, or leak text.
        if authorized:
            try:
                atomic_write(storage / "hook-status.json", encode({"schema": 1, "state": "collection-error", "updatedAt": utc_now()}))
            except Exception:
                pass  # Even the local error marker can fail on a full/unavailable disk.
    sys.stdout.buffer.write(b"{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
