#!/usr/bin/env python3
"""Run UDP error tests using the same SMOL_UDP_* inputs as host-udp/run.py."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('out', type=Path)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if args.native and sys.platform != 'linux':
        parser.error('--native requires Linux')
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='smol-udp-errors-') as tmp:
        root = Path(tmp)
        shutil.copy2(os.environ['SMOL_UDP_PROBE'], root/'probe')
        (root/'probe').chmod(0o755)
        config, reservations = [], []
        for label, family, ip in [('v4', socket.AF_INET, '127.0.0.1'),
                                   ('v6', socket.AF_INET6, '::1'),
                                   ('mapped-v4', socket.AF_INET, '127.0.0.1')]:
            for mode in ['recv', 'so-error', 'send', 'nonblocking', 'epoll']:
                reserve = socket.socket(family, socket.SOCK_DGRAM)
                reserve.bind((ip, 0))
                port = reserve.getsockname()[1]
                reservations.append(reserve)
                host = '::ffff:127.0.0.1' if label == 'mapped-v4' else ip
                peer = ('[%s]:%d' if ':' in host else '%s:%d') % (host, port)
                config.append('%s-%s %s %s' % (label, mode, mode, peer))
        (root/'udp-error-config').write_text('\n'.join(config)+'\n')
        env = os.environ.copy()
        env.update(SMOL_HOST_MODE='host', SMOL_DNS_FORWARDER='127.0.0.1:9')
        env.pop('SMOL_TCP_INTERCEPT', None)
        if args.native:
            command = [str(root/'probe')]
        else:
            library = Path(env['SMOL_UDP_LIBRARY']).resolve()
            dirs = [str(library.parent), *env.get('SMOL_UDP_LIB_DIR', '').split(os.pathsep)]
            search = os.pathsep.join(dict.fromkeys(d for d in dirs if d))
            env.update(LD_LIBRARY_PATH=search, DYLD_LIBRARY_PATH=search,
                       DYLD_FALLBACK_LIBRARY_PATH='/Library/Developer/CommandLineTools/usr/lib')
            command = [env['SMOL_UDP_LAUNCHER'], str(root), env['SMOL_UDP_KERNEL'], str(library)]
        for reserve in reservations:
            reserve.close()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.monotonic()
        try:
            with (args.out/'run.log').open('w') as log:
                run = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, timeout=30)
            code = run.returncode
        except subprocess.TimeoutExpired:
            code = -1
        elapsed = time.monotonic()-start
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        log = (args.out/'run.log').read_text()
        cases = [{'name': fields[1], 'passed': fields[2] == 'PASS'}
                 for line in log.splitlines() if line.startswith('UDP_ERROR_CASE ')
                 for fields in [line.split()]]
        loaded = [line.removeprefix('SMOL_RUNTIME_LIBRARY ') for line in log.splitlines()
                  if line.startswith('SMOL_RUNTIME_LIBRARY ')]
        verified = args.native or (len(loaded) == 1 and Path(loaded[0]).resolve() == library)
        result = {'passed': code == 0 and verified and [c['name'] for c in cases] == [line.split()[0] for line in config] and all(c['passed'] for c in cases),
                  'exit': code, 'mode': 'native-linux' if args.native else 'host-vm', 'cases': cases,
                  'elapsed_seconds': elapsed,
                  'child_cpu_seconds': after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
                  'runtime_verified': verified,
                  'hashes': {key: hashlib.sha256(Path(env[key]).read_bytes()).hexdigest()
                             for key in ['SMOL_UDP_PROBE', 'SMOL_UDP_KERNEL', 'SMOL_UDP_LIBRARY'] if key in env}}
        (args.out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result, indent=2))
        return 0 if result['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
