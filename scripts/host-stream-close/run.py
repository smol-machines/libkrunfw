#!/usr/bin/env python3
"""Verify host TCP close policy, delivery, and independent traffic through a VM."""
import argparse
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

MODES = ("graceful", "abort", "timeout", "fast", "concurrent", "unread")


def check_case(family, mode, root, out, native):
    address = "::1" if family == "v6" else "127.0.0.1"
    domain = socket.AF_INET6 if family == "v6" else socket.AF_INET
    closed = threading.Event()
    done = threading.Event()
    peer_ready = threading.Event()
    observed = {}
    live_peer = []
    errors = []
    data_listener = socket.socket(domain, socket.SOCK_STREAM)
    control_listener = socket.socket(domain, socket.SOCK_STREAM)
    for listener in (data_listener, control_listener):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        listener.bind((address, 0))
        listener.listen()
        listener.settimeout(15)
    (root / "close-config").write_text(
        f"{family} {data_listener.getsockname()[1]} "
        f"{control_listener.getsockname()[1]} {mode}\n"
    )

    def receive_data():
        try:
            with data_listener.accept()[0] as peer:
                peer.settimeout(15)
                live_peer.append(peer)
                peer_ready.set()
                assert peer.recv(1) == b"X"
                peer.sendall(b"AU" if mode == "unread" else b"A")
                if mode in ("graceful", "timeout", "concurrent"):
                    assert closed.wait(15), "guest did not complete close"
                    # Start after close, independently of startup/send duration.
                    time.sleep(6)
                observed["received"] = 0
                try:
                    while True:
                        data = peer.recv(65536)
                        if not data:
                            observed["close"] = "eof"
                            break
                        assert data == b"Z" * len(data)
                        observed["received"] += len(data)
                except ConnectionResetError:
                    observed["close"] = "reset"
        except BaseException:
            errors.append(traceback.format_exc())
        finally:
            done.set()

    def receive_control():
        try:
            with control_listener.accept()[0] as peer:
                peer.settimeout(20)
                while True:
                    opcode = peer.recv(1)
                    if opcode == b"P":
                        peer.sendall(b"P")
                    elif opcode == b"D":
                        if mode in ("graceful", "timeout", "concurrent"):
                            assert peer_ready.wait(2)
                            # XNU TCP_CONNECTION_INFO / Linux TCP_INFO expose
                            # state as their first byte, without consuming data.
                            option = 0x106 if sys.platform == "darwin" else 11
                            observed["peer_state_at_close"] = live_peer[0].getsockopt(
                                socket.IPPROTO_TCP, option, 256
                            )[0]
                        closed.set()
                        assert done.wait(15), "peer did not finish receiving"
                        peer.sendall(b"D")
                        break
                    else:
                        raise AssertionError(f"unexpected control message: {opcode!r}")
        except BaseException:
            errors.append(traceback.format_exc())

    threads = [threading.Thread(target=receive_data), threading.Thread(target=receive_control)]
    for thread in threads:
        thread.start()
    env = os.environ.copy()
    env.update(SMOL_HOST_MODE="host", SMOL_DNS_FORWARDER="127.0.0.1:9")
    env.pop("SMOL_TCP_INTERCEPT", None)
    library = None
    if native:
        command = [str(root / "probe")]
    else:
        library = Path(env["SMOL_UDP_LIBRARY"]).resolve()
        search = os.pathsep.join([str(library.parent), env["SMOL_UDP_LIB_DIR"]])
        env.update(LD_LIBRARY_PATH=search, DYLD_LIBRARY_PATH=search,
                   DYLD_FALLBACK_LIBRARY_PATH="/Library/Developer/CommandLineTools/usr/lib")
        command = [env["SMOL_UDP_LAUNCHER"], str(root), env["SMOL_UDP_KERNEL"], str(library)]
    log_path = out / f"{family}-{mode}.log"
    try:
        with log_path.open("w") as log:
            code = subprocess.run(command, cwd=root, env=env, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=30).returncode
    except subprocess.TimeoutExpired:
        code = -1
    finally:
        closed.set()
        for thread in threads:
            thread.join(timeout=20)
        data_listener.close()
        control_listener.close()
    assert all(not thread.is_alive() for thread in threads), "peer worker leaked"
    log = log_path.read_text()
    records = [line for line in log.splitlines() if line.startswith("CLOSE_RESULT ")]
    loaded = [line[len("SMOL_RUNTIME_LIBRARY "):] for line in log.splitlines()
              if line.startswith("SMOL_RUNTIME_LIBRARY ")]
    verified = native or (len(loaded) == 1 and Path(loaded[0]).resolve() == library)
    metrics = {}
    if len(records) == 1:
        metrics = {key: int(value) for key, value in
                   (field.split("=") for field in records[0].split()[1:])}
    passed = code == 0 and verified and not errors and bool(metrics)
    if passed:
        expected = "reset" if mode in ("abort", "unread") else "eof"
        passed = (observed.get("close") == expected and
                  observed.get("received") == metrics["accepted"])
        close_ms = metrics["close_us"] / 1000
        if mode in ("timeout", "concurrent"):
            # Linger can finish before its deadline if the peer TCP stack
            # acknowledged FIN, even while its application has not read data.
            fin_received = observed.get("peer_state_at_close") == (
                5 if sys.platform == "darwin" else 8
            )
            passed &= 500 < close_ms < 2000 or (close_ms < 500 and fin_received)
        else:
            passed &= close_ms < 500
        if mode == "concurrent":
            passed &= metrics["waiting"] == 1 and metrics["echo_us"] < 250000
    return {"name": f"{family}-{mode}", "passed": bool(passed), "exit": code,
            "runtime_verified": verified, "metrics": metrics, "peer": observed,
            "errors": errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--case", choices=[f"{f}-{m}" for f in ("v4", "v6") for m in MODES])
    args = parser.parse_args()
    if args.native and sys.platform != "linux":
        parser.error("the native control must use Linux socket semantics")
    args.out.mkdir(parents=True, exist_ok=False)
    result = {"passed": False, "cases": [], "hashes": {
        key: hashlib.sha256(Path(os.environ[key]).read_bytes()).hexdigest()
        for key in ("SMOL_UDP_PROBE", "SMOL_UDP_LIBRARY", "SMOL_UDP_KERNEL") if key in os.environ
    }}
    for family in ("v4", "v6"):
        for mode in MODES:
            if args.case and args.case != f"{family}-{mode}":
                continue
            with tempfile.TemporaryDirectory(prefix="smol-close-", dir="/tmp") as tmp:
                root = Path(tmp)
                shutil.copy2(os.environ["SMOL_UDP_PROBE"], root / "probe")
                (root / "probe").chmod(0o755)
                case = check_case(family, mode, root, args.out, args.native)
                result["cases"].append(case)
                print(json.dumps(case), flush=True)
            (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    result["passed"] = bool(result["cases"]) and all(case["passed"] for case in result["cases"])
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
