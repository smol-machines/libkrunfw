#!/usr/bin/env python3
"""Exercise asynchronous host TCP connects in a real libkrun VM.

Build guest.rs with rustc --target <guest-arch>-unknown-linux-musl, and compile
launcher.c against libkrun.h (link libdl on Linux; sign with HVF entitlements
on macOS). Set SMOL_ASYNC_PROBE, SMOL_ASYNC_LAUNCHER, SMOL_ASYNC_KERNEL and
SMOL_ASYNC_LIBRARY to those artifacts. Run with an output directory argument
and SMOL_ASYNC_IP=127.0.0.1, then SMOL_ASYNC_IP=::1.
SMOL_ASYNC_INTERCEPT_IP can select the other loopback family; SMOL_ASYNC_BIND
can be none, loopback, or wildcard to verify explicit source reservations.

The controlled interceptor withholds connection results until the guest
explicitly releases them. A blocking implementation therefore fails instead
of appearing correct just because loopback connects quickly.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback


def read_exact(conn, count):
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise RuntimeError("unexpected EOF in intercept exchange")
        data += chunk
    return data


def expect_closed(conn):
    try:
        data = conn.recv(1)
    except ConnectionResetError:
        data = b""
    assert data == b"", "pending connection was not released"


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    errors, records, threads = [], [], []
    stop = threading.Event()
    ip = os.environ["SMOL_ASYNC_IP"]
    assert ip in ["127.0.0.1", "::1"]
    intercept_ip = os.environ.get("SMOL_ASYNC_INTERCEPT_IP", ip)
    assert intercept_ip in ["127.0.0.1", "::1"]
    family = socket.AF_INET6 if ":" in intercept_ip else socket.AF_INET

    with tempfile.TemporaryDirectory(prefix="smol-async-connect-") as tmp:
        root = Path(tmp)
        shutil.copy2(os.environ["SMOL_ASYNC_PROBE"], root / "probe")
        (root / "probe").chmod(0o755)
        (root / "ip").write_text(ip)
        (root / "bind").write_text(os.environ.get("SMOL_ASYNC_BIND", "none"))
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.bind((intercept_ip, 0))
        listener.listen(32)
        listener.settimeout(0.2)

        def handle(conn):
            try:
                with conn:
                    conn.settimeout(6)
                    header = read_exact(conn, 44)
                    assert header[:9] == b"SMOLICPT\x01"
                    assert header[41] in [4, 6]
                    read_exact(conn, 4 if header[41] == 4 else 16)
                    case = (root / "case").read_text()
                    (root / "accepted").write_text("1")
                    if case in ["cancel", "shutdown", "signal"]:
                        expect_closed(conn)
                    else:
                        start = time.monotonic()
                        while not (root / "release").exists():
                            if stop.is_set() or time.monotonic() - start > 5:
                                raise TimeoutError("connect did not return: " + case)
                            time.sleep(0.002)
                        try:
                            conn.sendall(bytes([111 if case == "error" else 0]))
                        except (BrokenPipeError, ConnectionResetError):
                            # Closing before the verdict wins this race too.
                            if case != "race":
                                raise
                        if case in ["error", "race"]:
                            expect_closed(conn)
                        else:
                            assert read_exact(conn, 4) == b"PING"
                            # Data must arrive after the connect event was consumed.
                            time.sleep(0.05)
                            conn.sendall(b"PONG")
                records.append({"case": case, "family": header[41], "passed": True})
            except Exception:
                errors.append(traceback.format_exc())
            finally:
                (root / "closed").write_text("1")

        def accept():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                worker = threading.Thread(target=handle, args=(conn,), daemon=True)
                threads.append(worker)
                worker.start()

        acceptor = threading.Thread(target=accept)
        acceptor.start()
        env = os.environ.copy()
        endpoint = "[%s]:%d" if family == socket.AF_INET6 else "%s:%d"
        env["SMOL_TCP_INTERCEPT"] = endpoint % (intercept_ip, listener.getsockname()[1])
        env["SMOL_DNS_FORWARDER"] = "127.0.0.1:9"
        mode = env.get("SMOL_HOST_MODE", "host")
        assert mode in ["host", "legacy"]
        env["SMOL_HOST_MODE"] = mode
        try:
            with (out / "vm.log").open("w") as log:
                run = subprocess.run(
                    [env["SMOL_ASYNC_LAUNCHER"], tmp, env["SMOL_ASYNC_KERNEL"],
                     env["SMOL_ASYNC_LIBRARY"]],
                    env=env, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, timeout=60,
                )
            code = run.returncode
        except subprocess.TimeoutExpired:
            code = -1
            errors.append("VM timed out")
        finally:
            stop.set()
            acceptor.join(timeout=1)
            listener.close()
            for worker in threads:
                worker.join(timeout=7)
        content = (out / "vm.log").read_text()
        loaded = [line[len("SMOL_RUNTIME_LIBRARY "):] for line in content.splitlines()
                  if line.startswith("SMOL_RUNTIME_LIBRARY ")]
        runtime_verified = (len(loaded) == 1 and Path(loaded[0]).resolve()
                            == Path(env["SMOL_ASYNC_LIBRARY"]).resolve())
        result = {
            "mode": mode,
            "runtime_verified": runtime_verified,
            "exit": code,
            "passed": code == 0 and runtime_verified and "ASYNC_CONNECT_VM_PASS" in content
                      and len(records) == 50 and not errors,
            "guest_cases": [line for line in content.splitlines()
                            if line.startswith("ASYNC ")],
            "host_cases": records,
            "errors": errors,
            "hashes": {
                key: hashlib.sha256(Path(os.environ[key]).read_bytes()).hexdigest()
                for key in ["SMOL_ASYNC_PROBE", "SMOL_ASYNC_KERNEL", "SMOL_ASYNC_LIBRARY"]
            },
        }
        (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
