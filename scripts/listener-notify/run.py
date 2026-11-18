#!/usr/bin/env python3
"""Exercise the pollable listener ABI through TSI and native inet in real VMs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

p = argparse.ArgumentParser()
for key in ['probe', 'launcher', 'library', 'kernel', 'firmware-dir', 'out']:
    p.add_argument('--' + key, type=Path, required=True)
a = p.parse_args()
a.out.mkdir(parents=True, exist_ok=False)
result = {'cases': [], 'hashes': {key: hashlib.sha256(getattr(a, key).read_bytes()).hexdigest()
                               for key in ['probe', 'launcher', 'library', 'kernel']}}
for mode in ['host', 'legacy']:
    with tempfile.TemporaryDirectory(prefix='smol-notify-') as tmp:
        root = Path(tmp)
        shutil.copy2(a.probe, root / 'probe')
        env = os.environ.copy()
        env.pop('SMOL_TCP_INTERCEPT', None)
        search = os.pathsep.join([str(a.library.resolve().parent), str(a.firmware_dir.resolve())])
        env.update(SMOL_HOST_MODE=mode, SMOL_DNS_FORWARDER='127.0.0.1:9',
                   LD_LIBRARY_PATH=search, DYLD_LIBRARY_PATH=search)
        with (a.out / (mode + '.log')).open('w') as log:
            try:
                code = subprocess.run([str(a.launcher.resolve()), tmp, str(a.kernel.resolve()),
                                       str(a.library.resolve())], env=env, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=30).returncode
            except subprocess.TimeoutExpired:
                code = -1
        text = (a.out / (mode + '.log')).read_text()
        loaded = [line.removeprefix('SMOL_RUNTIME_LIBRARY ') for line in text.splitlines()
                  if line.startswith('SMOL_RUNTIME_LIBRARY ')]
        verified = len(loaded) == 1 and Path(loaded[0]).resolve() == a.library.resolve()
        passed = code == 0 and verified and 'NOTIFY_RESULT native passed' in text
        result['cases'].append({'mode': mode, 'exit': code, 'passed': passed,
                                'runtime_verified': verified})
        print(mode, 'PASS' if passed else 'FAIL', flush=True)
result['passed'] = all(case['passed'] for case in result['cases'])
(a.out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
raise SystemExit(not result['passed'])
