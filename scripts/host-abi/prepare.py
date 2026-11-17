#!/usr/bin/env python3
"""Prepare ABI tests: prepare.py <x86|arm> <old-kernel> <new-kernel> <output>.

Old must lack the current host ABI marker; new includes patch 0058.
Use ELF vmlinux for x86 and raw Image for arm. Requires zstd on x86.
Build guest.rs with rustc --edition=2021 --target <arch>-unknown-linux-musl.
Compile launcher.c against the matching libkrun.h, link libdl on Linux,
and ad-hoc sign the macOS launcher with libkrun's HVF entitlements.
Compile bundle.c as libkrunfw.so.5 (-shared -fPIC on Linux) or
libkrunfw.5.dylib (-dynamiclib on macOS), placing it in output.

Run run.py <results> with SMOL_ABI_ARCH, SMOL_ABI_ARTIFACTS (output),
SMOL_ABI_LIBRARY, SMOL_ABI_LAUNCHER and SMOL_ABI_PROBE set to these artifacts.
"""
import bz2
import gzip
from pathlib import Path
import subprocess
import sys


def main():
    arch, old, new, output = sys.argv[1:]
    assert arch in ["x86", "arm"]
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    old, new = Path(old).read_bytes(), Path(new).read_bytes()
    marker = b"KRUN_TSI_HOST_NETWORK_ABI_7\0"
    assert marker not in old
    assert new.count(marker) == 1
    for name, data in [("old", old), ("new", new)]:
        path = out / (name + "-" + arch)
        path.write_bytes(data)
        Path(str(path) + ".gz").write_bytes(gzip.compress(data, compresslevel=1))
        if arch == "x86":
            Path(str(path) + ".bz2").write_bytes(bz2.compress(data, compresslevel=1))
            subprocess.run(["zstd", "-q", "-1", "-f", str(path), "-o", str(path) + ".zst"], check=True)
    (out / ("abi8-" + arch)).write_bytes(new.replace(marker, b"KRUN_TSI_HOST_NETWORK_ABI_8\0"))
    if arch == "x86":
        (out / "appended-x86").write_bytes(old + marker)


if __name__ == "__main__":
    main()
