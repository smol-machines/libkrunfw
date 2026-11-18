#!/usr/bin/env python3
"""Verify listener state in native Linux or real TSI host/legacy guests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--case', choices=['state', 'all'], default='all')
    parser.add_argument('--launcher', type=Path)
    parser.add_argument('--library', type=Path)
    parser.add_argument('--kernel', type=Path)
    parser.add_argument('--firmware-dir', type=Path)
    args = parser.parse_args()
    if not args.native and not all([args.launcher, args.library, args.kernel, args.firmware_dir]):
        parser.error('guest tests require launcher, library, kernel and firmware-dir')
    args.out.mkdir(parents=True, exist_ok=False)
    result = {'scope': args.case, 'cases': [], 'hashes': {}}
    for key in ['probe', 'launcher', 'library', 'kernel']:
        path = getattr(args, key)
        if path:
            result['hashes'][key] = hashlib.sha256(path.read_bytes()).hexdigest()
    for mode in ['native'] if args.native else ['host', 'legacy']:
        with tempfile.TemporaryDirectory(prefix='smol-listener-') as tmp:
            root = Path(tmp)
            (root / 'listener-config').write_text(args.case)
            shutil.copy2(args.probe, root / 'probe')
            env = os.environ.copy()
            env.pop('SMOL_TCP_INTERCEPT', None)
            env.update(SMOL_HOST_MODE=mode, SMOL_DNS_FORWARDER='127.0.0.1:9')
            if args.native:
                command = [str(root / 'probe')]
            else:
                search = os.pathsep.join([str(args.library.resolve().parent), str(args.firmware_dir.resolve())])
                env.update(LD_LIBRARY_PATH=search, DYLD_LIBRARY_PATH=search)
                command = [str(args.launcher.resolve()), str(root), str(args.kernel.resolve()), str(args.library.resolve())]
            with (args.out / (mode + '.log')).open('w') as log:
                try:
                    code = subprocess.run(command, env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=30).returncode
                except subprocess.TimeoutExpired:
                    code = -1
            log = (args.out / (mode + '.log')).read_text()
            markers = [line for line in log.splitlines() if line.startswith('LISTENER_RESULT ')]
            loaded = [line[len('SMOL_RUNTIME_LIBRARY '):] for line in log.splitlines() if line.startswith('SMOL_RUNTIME_LIBRARY ')]
            runtime_verified = args.native or (len(loaded) == 1 and Path(loaded[0]).resolve() == args.library.resolve())
            passed = code == 0 and runtime_verified and markers == ['LISTENER_RESULT 127.0.0.1 passed', 'LISTENER_RESULT ::1 passed']
            result['cases'].append({'mode':mode, 'exit':code, 'passed':passed, 'runtime_verified':runtime_verified})
            print(mode, 'PASS' if passed else 'FAIL', flush=True)
    result['passed'] = all(case['passed'] for case in result['cases'])
    (args.out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    raise SystemExit(not result['passed'])


if __name__ == '__main__':
    main()
