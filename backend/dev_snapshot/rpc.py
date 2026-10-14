"""Private stdio JSON-RPC 2.0 transport. No sockets, network server, or code evaluation."""
from __future__ import annotations

import json
import queue
import sys
import threading

from .common import SafetyError, encode
from .engine import Engine

MAX_REQUEST = 2 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024


def main() -> None:
    requests: queue.Queue = queue.Queue(maxsize=1000)
    output_lock = threading.Lock()

    def send(value: dict) -> None:
        data = encode(value)
        if len(data) > MAX_RESPONSE:
            data = encode({"jsonrpc": "2.0", "id": value.get("id"), "error": {"code": -32000, "message": "Response exceeds protocol size limit."}})
        with output_lock:
            sys.stdout.buffer.write(data + b"\n")
            sys.stdout.buffer.flush()

    def notify(method: str, params: dict) -> None:
        send({"jsonrpc": "2.0", "method": method, "params": params})

    engine = Engine(notify)

    def reader() -> None:
        try:
            while True:
                line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
                if not line:
                    break
                if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                    requests.put({"invalid": True})
                    break
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str) or not isinstance(message.get("params", {}), dict):
                        raise ValueError()
                    params = message.get("params", {})
                    # These lightweight notifications update capture guards even during a long operation.
                    if message["method"] == "fileEvent" and "id" not in message:
                        engine.changed(params["repositoryId"], params.get("path"))
                        continue
                    if message["method"] == "dirty" and "id" not in message:
                        engine.dirty(params["repositoryId"], params["paths"])
                        continue
                    requests.put(message)
                except (ValueError, KeyError, TypeError, SafetyError):
                    requests.put({"invalid": True})
        finally:
            requests.put(None)

    threading.Thread(target=reader, daemon=True, name="dev-snapshot-stdio").start()
    try:
        while True:
            try:
                request = requests.get(timeout=0.2)
            except queue.Empty:
                engine.tick()
                continue
            if request is None:
                break
            if request.get("invalid"):
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid or oversized request."}})
                continue
            request_id = request.get("id")
            if type(request_id) not in (int, str) and request_id is not None:
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request ID."}})
                continue
            if request["method"] == "shutdown":
                send({"jsonrpc": "2.0", "id": request_id, "result": None})
                break
            try:
                result = engine.call(request["method"], request.get("params", {}))
                if request_id is not None:
                    send({"jsonrpc": "2.0", "id": request_id, "result": result})
            except Exception as error:
                message = str(error) if isinstance(error, SafetyError) else "Operation failed. Existing snapshots and recovery records have been preserved."
                if request_id is not None:
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": message}})
            engine.tick()
    finally:
        engine.close()
