#!/usr/bin/env python3
"""Real VM ABI/mode matrix; artifacts and paths supplied through SMOL_ABI_*.

The guest binds ports already owned by this process. Host mode must reject
them; legacy mode must accept them in its isolated namespace. A successful
boot alone is insufficient. Every rejected image must leave guest-ran absent.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile


def flatten(path, output, arch):
    data = path.read_bytes()
    if arch == "arm":
        load = entry = 0x80000000
        result = data
    else:
        header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
        entry, phoff, phsize, count = header[4], header[5], header[9], header[10]
        segments = []
        for i in range(count):
            ptype, _, offset, _, address, size, _, _ = struct.unpack_from(
                "<IIQQQQQQ", data, phoff + i * phsize)
            if ptype == 1:
                segments.append((address, offset, size))
        load = min(address for address, _, _ in segments)
        end = max(address + size for address, _, size in segments)
        result = bytearray(end - load)
        for address, offset, size in segments:
            result[address - load:address - load + size] = data[offset:offset + size]
    result += bytes((-len(result)) % 65536)
    output.write_bytes(result)
    return {"SMOL_ABI_RAW": str(output), "SMOL_ABI_LOAD": str(load),
            "SMOL_ABI_ENTRY": str(entry)}


def main():
    artifacts = Path(os.environ["SMOL_ABI_ARTIFACTS"])
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    arch = os.environ["SMOL_ABI_ARCH"]
    env = os.environ.copy()
    env["SMOL_DNS_FORWARDER"] = "127.0.0.1:9"
    # macOS removes DYLD_* when launching /usr/bin/python3; set these here
    # so the unsigned test fixture is found by the signed native launcher.
    env["DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"] = str(artifacts)
    if sys.platform == "darwin":
        env["DYLD_FALLBACK_LIBRARY_PATH"] = "/Library/Developer/CommandLineTools/usr/lib"
    cases = []
    basic = "reboot=k panic=-1 console=hvc0 rootfstype=virtiofs root=/dev/root rw init=/init.krun"
    for mode in ["host", "legacy"]:
        for cmd in ["omitted", "opposite", "separator"]:
            override = "0" if mode == "host" else "1"
            command = basic if cmd == "omitted" else basic + " tsi_host_network=" + override
            if cmd == "separator":
                command += ' option="before -- after" -- tsi_host_network=' + override
            cases.append(("new-" + mode + "-" + cmd, "new", mode,
                          {"SMOL_ABI_CMDLINE": command}, None))
    cases += [("old-host", "old", "host", {}, errno.EOPNOTSUPP),
              ("old-legacy", "old", "legacy", {}, None),
              ("abi8-host", "abi8", "host", {}, errno.EOPNOTSUPP),
              ("abi8-legacy", "abi8", "legacy", {}, None),
              ("port-map", "new", "host", {"SMOL_ABI_PORT_MAP": "1"}, errno.EINVAL)]
    if arch == "x86":
        cases.append(("appended-host", "appended", "host", {}, errno.EOPNOTSUPP))
    compressed = [("gz", "2")] if arch == "arm" else [("bz2", "3"), ("gz", "4"), ("zst", "5")]
    for suffix, fmt in compressed:
        for version, expected in [("new", None), ("old", errno.EOPNOTSUPP)]:
            cases.append((version + "-" + suffix, version, "host",
                          {"SMOL_KERNEL_FORMAT": fmt, "suffix": "." + suffix}, expected))
    for version in ["new", "old", "abi8"]:
        extra = flatten(artifacts / (version + "-" + arch), out / (version + ".raw"), arch)
        extra["SMOL_ABI_BUNDLE"] = "1"
        for mode in ["host", "legacy"]:
            expected = errno.EOPNOTSUPP if mode == "host" and version != "new" else None
            cases.append(("bundle-" + version + "-" + mode, version, mode, extra, expected))
    if os.environ.get("SMOL_ABI_CASE_PREFIX"):
        cases = [case for case in cases if case[0].startswith(os.environ["SMOL_ABI_CASE_PREFIX"])]
    assert cases, "case filter selected no tests"
    results = []
    for name, version, mode, extra, expected in cases:
        with tempfile.TemporaryDirectory(prefix="smol-host-abi-") as tmp:
            root = Path(tmp)
            shutil.copy2(env["SMOL_ABI_PROBE"], root / "probe")
            (root / "probe").chmod(0o755)
            reservations = []
            addresses = []
            for family, ip in [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]:
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.bind((ip, 0))
                sock.listen()
                reservations.append(sock)
                addresses.append(("[%s]:%d" if family == socket.AF_INET6 else "%s:%d") % (ip, sock.getsockname()[1]))
            (root / "mode-config").write_text(mode + " " + " ".join(addresses))
            run_env = dict(env, SMOL_HOST_MODE=mode)
            run_env.update({k: v for k, v in extra.items() if k != "suffix"})
            image = artifacts / (version + "-" + arch + extra.get("suffix", ""))
            command = [env["SMOL_ABI_LAUNCHER"], tmp, str(image), env["SMOL_ABI_LIBRARY"]]
            try:
                run = subprocess.run(command, env=run_env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
                log = run.stdout.decode(errors="replace")
                code = run.returncode
            except subprocess.TimeoutExpired as exc:
                log = (exc.stdout or b"").decode(errors="replace")
                code = -1
            finally:
                for sock in reservations:
                    sock.close()
            (out / (name + ".log")).write_text(log)
            ran = (root / "guest-ran").exists()
            if expected is None:
                passed = code == 0 and ran and "HOST_ABI_VM_PASS mode=" + mode in log
            else:
                message = "port publishing map" if expected == errno.EINVAL else "host-network ABI 7"
                passed = code == 2 and not ran and "krun_start_enter: -%d:" % expected in log and message in log
            passed = passed and "RUNTIME_HOST_ABI=7" in log
            results.append({"name": name, "passed": passed, "exit": code, "guest_ran": ran})
            print(name, "PASS" if passed else "FAIL", flush=True)
            (out / "result.json").write_text(json.dumps({"cases": results, "passed": all(r["passed"] for r in results),
                "hashes": {k: hashlib.sha256(Path(env[k]).read_bytes()).hexdigest()
                           for k in ["SMOL_ABI_LIBRARY", "SMOL_ABI_PROBE", "SMOL_ABI_LAUNCHER"]}}, indent=2))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
