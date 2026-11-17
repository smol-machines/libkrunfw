#!/usr/bin/env python3
"""Check that UDP receive-buffer settings affect queued traffic, not only readback.

Compile guest.rs for the host's Linux musl architecture. The SMOL_UDP_PROBE,
SMOL_UDP_LAUNCHER, SMOL_UDP_KERNEL, SMOL_UDP_LIBRARY and SMOL_UDP_LIB_DIR
variables select isolated artifacts. Use --native for a native Linux baseline.
"""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('out', type=pathlib.Path)
parser.add_argument('--native', action='store_true')
args = parser.parse_args()
if args.native and sys.platform != 'linux':
    parser.error('native baseline requires Linux')
args.out.mkdir(parents=True, exist_ok=False)
stop = threading.Event()
sockets, threads, errors, config, spec = [], [], [], [], []
# Forced receive limits require CAP_NET_ADMIN, provided by the test guest.
phases = ['prebind', 'bound', 'connected'] + ([] if args.native else ['forced'])


def receive_address(listener):
    while not stop.is_set():
        try:
            control, _ = listener.accept()
            return control
        except socket.timeout:
            continue
    raise RuntimeError('probe stopped before connecting')


def burst(name, listener, sender):
    try:
        with receive_address(listener) as control:
            control.settimeout(5)
            address = b''
            while not address.endswith(b'\n'):
                part = control.recv(1)
                if not part:
                    raise RuntimeError('missing UDP address')
                address += part
                if len(address) > 128:
                    raise RuntimeError('invalid UDP address')
            host, port = address.decode().strip().rsplit(':', 1)
            destination = (host.strip('[]'), int(port))
            for sequence in range(512):
                sender.sendto(sequence.to_bytes(4, 'little') + bytes(1020), destination)
                time.sleep(.0002)
            control.sendall(b'!')
    except Exception:
        errors.append(dict(name=name, error=traceback.format_exc()))


try:
    for family, ip, label in [(socket.AF_INET, '127.0.0.1', 'v4'),
                              (socket.AF_INET6, '::1', 'v6')]:
        for phase in phases:
            for value in [4096, 1048576, 2097152]:
                name = '%s-%s-%d' % (label, phase, value)
                listener = socket.socket(family, socket.SOCK_STREAM)
                sender = socket.socket(family, socket.SOCK_DGRAM)
                sockets.extend([listener, sender])
                listener.bind((ip, 0))
                listener.listen(1)
                listener.settimeout(.1)
                sender.bind((ip, 0))
                fmt = '[%s]:%d' if family == socket.AF_INET6 else '%s:%d'
                peers = [fmt % (ip, s.getsockname()[1]) for s in [listener, sender]]
                config.append('%s %s %d %s %s' % (name, phase, value, *peers))
                spec.append(dict(name=name, family=label, phase=phase, requested=value))
                thread = threading.Thread(target=burst, args=(name, listener, sender))
                threads.append(thread)
                thread.start()
    with tempfile.TemporaryDirectory(prefix='smol-dgram-buffer-') as tmp:
        root = pathlib.Path(tmp)
        shutil.copy2(os.environ['SMOL_UDP_PROBE'], root / 'probe')
        (root / 'probe').chmod(0o755)
        (root / 'buffer-config').write_text('\n'.join(config) + '\n')
        env = os.environ.copy()
        env.update(SMOL_HOST_MODE='host', SMOL_DNS_FORWARDER='127.0.0.1:9')
        env.pop('SMOL_TCP_INTERCEPT', None)
        if args.native:
            command = [str(root / 'probe')]
        else:
            library = pathlib.Path(env['SMOL_UDP_LIBRARY']).resolve()
            search = os.pathsep.join([str(library.parent), env['SMOL_UDP_LIB_DIR']])
            env.update(LD_LIBRARY_PATH=search, DYLD_LIBRARY_PATH=search,
                       DYLD_FALLBACK_LIBRARY_PATH='/Library/Developer/CommandLineTools/usr/lib')
            command = [env['SMOL_UDP_LAUNCHER'], str(root), env['SMOL_UDP_KERNEL'], str(library)]
        try:
            with (args.out / 'run.log').open('w') as log:
                code = subprocess.run(command, cwd=root, env=env, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=60).returncode
        except subprocess.TimeoutExpired:
            code = -1
finally:
    stop.set()
    for thread in threads:
        thread.join(timeout=6)
    for s in sockets:
        s.close()
log = (args.out / 'run.log').read_text()
found = {fields[1]: dict(actual=int(fields[2]), received=int(fields[3]))
         for line in log.splitlines() if line.startswith('BUFFER_CASE ')
         for fields in [line.split()]}
cases = [dict(case, **found.get(case['name'], {})) for case in spec]
checks = []
for family in ['v4', 'v6']:
    for phase in phases:
        small = found.get('%s-%s-4096' % (family, phase), {})
        medium = found.get('%s-%s-1048576' % (family, phase), {})
        large = found.get('%s-%s-2097152' % (family, phase), {})
        # Virtio and native UDP account different per-packet allocations.
        # Keep the intermediate count visible, then require a complete burst
        # at a size that accommodates both transports' packet overhead.
        passed = (bool(small and medium and large) and small['actual'] == 8192
                  and medium['actual'] == 2097152 and large['actual'] == 4194304
                  and large['received'] == 512
                  and 0 < small['received'] < medium['received'] // 4)
        checks.append(dict(name=family + '-' + phase, passed=passed))
loaded = [line[len('SMOL_RUNTIME_LIBRARY '):] for line in log.splitlines()
          if line.startswith('SMOL_RUNTIME_LIBRARY ')]
verified = args.native or (len(loaded) == 1 and pathlib.Path(loaded[0]).resolve() == library)
report = dict(passed=code == 0 and verified and not errors and set(found) == {case['name'] for case in spec}
              and all(c['passed'] for c in checks), exit=code, cases=cases, checks=checks,
              errors=errors, runtime_verified=verified,
              hashes={k: hashlib.sha256(pathlib.Path(env[k]).read_bytes()).hexdigest()
                      for k in ['SMOL_UDP_PROBE', 'SMOL_UDP_LIBRARY', 'SMOL_UDP_KERNEL'] if k in env})
(args.out / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
sys.exit(0 if report['passed'] else 1)
