#!/usr/bin/env python3
"""Exercise real read/write deadlines with connected and accepted TCP sockets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--case", default="")
    args = parser.parse_args()
    if args.native and sys.platform != "linux":
        parser.error("native baseline requires Linux")
    expected = [
        f"{family}-{phase}-{operation}"
        for family in ["v4", "v6"]
        for phase in ["connected", "preconnect", "accepted", "inherited"]
        for operation in ["read", "write"]
    ]
    if args.case:
        if args.case not in expected:
            parser.error("unknown timeout case: " + args.case)
        expected = [args.case]
    args.out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="smol-stream-timeouts-") as tmp:
        root = Path(tmp)
        shutil.copy2(os.environ["SMOL_UDP_PROBE"], root / "probe")
        (root / "probe").chmod(0o755)
        (root / "timeout-filter").write_text(args.case)
        env = os.environ.copy()
        env.update(SMOL_HOST_MODE="host", SMOL_DNS_FORWARDER="127.0.0.1:9")
        env.pop("SMOL_TCP_INTERCEPT", None)
        if args.native:
            command = [str(root / "probe")]
        else:
            library = Path(env["SMOL_UDP_LIBRARY"]).resolve()
            search = os.pathsep.join([str(library.parent), env["SMOL_UDP_LIB_DIR"]])
            env.update(
                LD_LIBRARY_PATH=search,
                DYLD_LIBRARY_PATH=search,
                DYLD_FALLBACK_LIBRARY_PATH="/Library/Developer/CommandLineTools/usr/lib",
            )
            command = [env["SMOL_UDP_LAUNCHER"], str(root),
                       env["SMOL_UDP_KERNEL"], str(library)]
        try:
            with (args.out / "run.log").open("w") as log:
                code = subprocess.run(
                    command, cwd=root, env=env, stdout=log,
                    stderr=subprocess.STDOUT, timeout=8 if args.case else 40,
                ).returncode
        except subprocess.TimeoutExpired:
            code = -1

    log = (args.out / "run.log").read_text()
    cases = []
    loaded = []
    for line in log.splitlines():
        if line.startswith("TIMEOUT_CASE "):
            _, name, status, elapsed, transferred = line.split()
            cases.append({"name": name, "passed": status == "PASS",
                          "elapsed_ms": int(elapsed), "transferred": int(transferred)})
        elif line.startswith("SMOL_RUNTIME_LIBRARY "):
            loaded.append(line[len("SMOL_RUNTIME_LIBRARY "):])
    verified = args.native or (len(loaded) == 1 and Path(loaded[0]).resolve() == library)
    result = {
        "passed": (code == 0 and verified and [case["name"] for case in cases] == expected
                   and all(case["passed"] for case in cases)),
        "exit": code,
        "runtime_verified": verified,
        "cases": cases,
        "hashes": {
            key: hashlib.sha256(Path(env[key]).read_bytes()).hexdigest()
            for key in ["SMOL_UDP_PROBE", "SMOL_UDP_LIBRARY", "SMOL_UDP_KERNEL"]
            if key in env
        },
    }
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
