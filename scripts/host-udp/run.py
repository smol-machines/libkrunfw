#!/usr/bin/env python3
"""Compare Linux UDP semantics natively and through real host-mode VMs.

Compile guest.rs with rustc --edition=2021 --target <arch>-unknown-linux-musl
-C linker=rust-lld -o /path/to/probe. Set SMOL_UDP_PROBE and run this script
with an output directory; add --native for the Linux baseline. VM runs also
require SMOL_UDP_LAUNCHER (../host-connect/launcher.c, HVF-signed on macOS),
SMOL_UDP_KERNEL and SMOL_UDP_LIBRARY. SMOL_UDP_LIB_DIR supplies shared libraries.

Each test owns its sockets and a temporary guest root. Alternate replies are
sent before a message from the connected peer; accepting the alternate reply
is a failure, even when sending to that destination succeeds.
"""
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
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--native", action="store_true")
    args = parser.parse_args()
    if args.native and sys.platform != "linux":
        parser.error("--native requires Linux; the assertions use Linux socket semantics")
    args.out.mkdir(parents=True, exist_ok=True)
    errors, records, threads, sockets, reservations = [], [], [], [], []
    connected_sources = {}
    stop = threading.Event()

    def echo(sock, peer=None):
        try:
            while not stop.is_set():
                try:
                    data, source = sock.recvfrom(128)
                except socket.timeout:
                    continue
                records.append({"data": data.decode(), "source": source,
                                "listener": sock.getsockname()})
                if peer is None and data == b"before":
                    connected_sources[sock.fileno()] = source
                if peer is not None and data == b"alternate":
                    assert source == connected_sources[peer.fileno()], (
                        "alternate send changed the actual source address", source,
                        connected_sources[peer.fileno()])
                    sock.sendto(b"wrong-peer", source)
                    peer.sendto(b"right-peer", source)
                else:
                    sock.sendto(data, source)
        except Exception:
            errors.append(traceback.format_exc())

    with tempfile.TemporaryDirectory(prefix="smol-udp-conformance-") as tmp:
        root = Path(tmp)
        probe = Path(os.environ["SMOL_UDP_PROBE"])
        shutil.copy2(probe, root / "probe")
        (root / "probe").chmod(0o755)
        config = []
        for label, family, ip, guest_ip, wildcard in [
                ("v4", socket.AF_INET, "127.0.0.1", "127.0.0.1", "0.0.0.0"),
                ("v6", socket.AF_INET6, "::1", "::1", "::"),
                ("mapped-v4", socket.AF_INET, "127.0.0.1", "::ffff:127.0.0.1", "::")]:
            def address(host, port):
                return ("[%s]:%d" if ":" in host else "%s:%d") % (host, port)

            peers = []
            for _ in range(2):
                sock = socket.socket(family, socket.SOCK_DGRAM)
                sock.bind((ip, 0))
                sock.settimeout(0.1)
                peers.append(sock)
                sockets.append(sock)
            for index, sock in enumerate(peers):
                thread = threading.Thread(target=echo, args=(sock, peers[0] if index else None))
                thread.start()
                threads.append(thread)
            for binding, host in [("local", guest_ip), ("wildcard", wildcard)]:
                for port_kind in ["ephemeral", "fixed"]:
                    for kind in ["alternate", "disconnect", "failed-family"]:
                        if kind == "failed-family" and label != "v4":
                            continue
                        port = 0
                        if port_kind == "fixed":
                            reserve = socket.socket(family, socket.SOCK_DGRAM)
                            reserve.bind((ip if binding == "local" else ("0.0.0.0" if family == socket.AF_INET else "::"), 0))
                            port = reserve.getsockname()[1]
                            reservations.append(reserve)
                        name = "-".join([kind, label,
                                         binding, port_kind])
                        config.append(" ".join([name, kind, address(host, port),
                                                address(guest_ip, peers[0].getsockname()[1]),
                                                address(guest_ip, peers[1].getsockname()[1])]))
        (root / "udp-config").write_text("\n".join(config) + "\n")
        env = os.environ.copy()
        env["SMOL_HOST_MODE"] = "host"
        env["SMOL_DNS_FORWARDER"] = "127.0.0.1:9"
        env.pop("SMOL_TCP_INTERCEPT", None)
        if not args.native:
            library = Path(env["SMOL_UDP_LIBRARY"]).resolve()
            env["SMOL_UDP_LIBRARY"] = str(library)
            search = [str(library.parent)]
            search.extend(env.get("SMOL_UDP_LIB_DIR", "").split(os.pathsep))
            search = os.pathsep.join(dict.fromkeys(path for path in search if path))
            env["LD_LIBRARY_PATH"] = search
            env["DYLD_LIBRARY_PATH"] = search
            env["DYLD_FALLBACK_LIBRARY_PATH"] = "/Library/Developer/CommandLineTools/usr/lib"
        if args.native:
            command = [str(root / "probe")]
        else:
            command = [env["SMOL_UDP_LAUNCHER"], str(root), env["SMOL_UDP_KERNEL"],
                       env["SMOL_UDP_LIBRARY"]]
        try:
            for sock in reservations:
                sock.close()
            with (args.out / "run.log").open("w") as log:
                run = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, timeout=90)
            code = run.returncode
        except subprocess.TimeoutExpired:
            code = -1
            errors.append("probe timed out")
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)
            for sock in sockets + reservations:
                sock.close()
        log = (args.out / "run.log").read_text()
        cases = [{"name": words[1], "passed": words[2] == "PASS"}
                 for line in log.splitlines() if line.startswith("UDP_CASE ")
                 for words in [line.split()]]
        expected_names = [line.split()[0] for line in config]
        loaded = [line.removeprefix("SMOL_RUNTIME_LIBRARY ")
                  for line in log.splitlines() if line.startswith("SMOL_RUNTIME_LIBRARY ")]
        if not args.native and (len(loaded) != 1 or
                                Path(loaded[0]).resolve() != library):
            errors.append("launcher did not verify the requested runtime library")
        result = {"passed": code == 0 and not errors
                            and [case["name"] for case in cases] == expected_names
                            and all(case["passed"] for case in cases),
                  "mode": "native-linux" if args.native else "host-vm",
                  "exit": code, "cases": cases, "expected_cases": len(config),
                  "errors": errors, "host_datagrams": records,
                  "loaded_runtime": loaded[0] if len(loaded) == 1 else None,
                  "hashes": {key: hashlib.sha256(Path(env[key]).read_bytes()).hexdigest()
                             for key in ["SMOL_UDP_PROBE", "SMOL_UDP_KERNEL", "SMOL_UDP_LIBRARY"]
                             if key in env}}
        (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({key: result[key] for key in ["passed", "exit", "cases", "errors"]}, indent=2))
        return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
