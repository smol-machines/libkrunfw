#!/usr/bin/env python3
"""Exercise connected UDP sockets over the legacy TSI transport in a real VM.

Compile guest.rs as a static binary (rustc --edition=2021 --target
<arch>-unknown-linux-musl) and ../host-connect/launcher.c against libkrun.h,
then pass both with the libkrun library and the guest kernel under test.

Each case runs in the guest's initial network namespace and again in a fresh
one, where a datagram connect only records the peer. The peers listen on this
host's outbound IPv4 address, because the guest keeps loopback to itself.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import traceback

KINDS = ["repeat", "batch", "alternate"]
NAMESPACES = ["initial", "nested"]


def outbound_ipv4():
    # Selects a source address; a datagram connect sends nothing.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]


def main():
    parser = argparse.ArgumentParser()
    for key in ["probe", "launcher", "library", "kernel", "out"]:
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    errors, records, threads, sockets = [], [], [], []
    stop = threading.Event()
    ip = outbound_ipv4()

    def serve(role, sock):
        try:
            while not stop.is_set():
                try:
                    data, source = sock.recvfrom(128)
                except socket.timeout:
                    continue
                records.append({"role": role, "data": data.decode()})
                if role == "peer":
                    sock.sendto(data, source)
        except Exception:
            errors.append(traceback.format_exc())

    with tempfile.TemporaryDirectory(prefix="smol-dgram-connect-") as tmp:
        root = Path(tmp)
        shutil.copy2(args.probe, root / "probe")
        (root / "probe").chmod(0o755)
        addresses = {}
        for role in ["peer", "alternate"]:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((ip, 0))
            sock.settimeout(0.1)
            sockets.append(sock)
            addresses[role] = "%s:%d" % sock.getsockname()
            thread = threading.Thread(target=serve, args=(role, sock))
            thread.start()
            threads.append(thread)
        config = [" ".join([kind + "-" + namespace, namespace, kind,
                            addresses["peer"], addresses["alternate"]])
                  for namespace in NAMESPACES for kind in KINDS]
        (root / "dgram-config").write_text("\n".join(config) + "\n")
        library = args.library.resolve()
        env = os.environ.copy()
        env.pop("SMOL_TCP_INTERCEPT", None)
        search = os.pathsep.join(dict.fromkeys(
            path for path in [str(library.parent), env.get("LD_LIBRARY_PATH", "")] if path))
        env.update(SMOL_HOST_MODE="legacy", SMOL_DNS_FORWARDER="127.0.0.1:9",
                   LD_LIBRARY_PATH=search, DYLD_LIBRARY_PATH=search)
        try:
            with (args.out / "run.log").open("w") as log:
                code = subprocess.run(
                    [str(args.launcher.resolve()), str(root), str(args.kernel.resolve()),
                     str(library)], cwd=root, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, timeout=90).returncode
        except subprocess.TimeoutExpired:
            code = -1
            errors.append("probe timed out")
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)
            for sock in sockets:
                sock.close()

    log = (args.out / "run.log").read_text()
    cases = [{"name": words[1], "passed": words[2] == "PASS"}
             for line in log.splitlines() if line.startswith("DGRAM_CASE ")
             for words in [line.split()]]
    loaded = [line.removeprefix("SMOL_RUNTIME_LIBRARY ")
              for line in log.splitlines() if line.startswith("SMOL_RUNTIME_LIBRARY ")]
    if len(loaded) != 1 or Path(loaded[0]).resolve() != library:
        errors.append("launcher did not verify the requested runtime library")
    # The alternate address hears its one datagram per case and nothing else.
    strays = [record for record in records
              if (record["role"] == "alternate") != (record["data"] == "alternate")]
    if strays:
        errors.append("datagrams reached the wrong address: %r" % strays)
    delivered = sum(record["role"] == "alternate" for record in records)
    if delivered != len(NAMESPACES):
        errors.append("alternate address received %d datagrams" % delivered)
    result = {"passed": code == 0 and not errors
                        and [case["name"] for case in cases] == [line.split()[0] for line in config]
                        and all(case["passed"] for case in cases),
              "exit": code, "cases": cases, "expected_cases": len(config),
              "errors": errors, "host_datagrams": records,
              "hashes": {key: hashlib.sha256(getattr(args, key).read_bytes()).hexdigest()
                         for key in ["probe", "launcher", "library", "kernel"]}}
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ["passed", "exit", "cases", "errors"]},
                     indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
