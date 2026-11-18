#!/usr/bin/env python3
"""Rewind a live guest after its listening port set changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    for name in ['out', 'probe', 'launcher', 'library', 'kernel', 'firmware-dir']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    result = {'checks': [], 'passed': False, 'hashes': {
        name: hashlib.sha256(getattr(args, name).read_bytes()).hexdigest()
        for name in ['probe', 'launcher', 'library', 'kernel']}}
    owned = []
    logs = []
    temporary = tempfile.TemporaryDirectory(prefix='smol-restore-')
    try:
        base = Path(temporary.name)
        root = base / 'root'
        root.mkdir()
        shutil.copy2(args.probe, root / 'probe')
        snapshot = base / 'snapshot'
        control = base / 'control.sock'
        env = os.environ.copy()
        env.pop('SMOL_TCP_INTERCEPT', None)
        env.pop('SMOL_TEST_SNAPSHOT', None)
        search = os.pathsep.join([str(args.library.resolve().parent), str(args.firmware_dir.resolve())])
        env.update(LD_LIBRARY_PATH=search, DYLD_LIBRARY_PATH=search,
                   SMOL_HOST_MODE='host', SMOL_DNS_FORWARDER='127.0.0.1:9')

        def start(name, restore=False):
            config = env.copy()
            config['SMOL_TEST_CONTROL'] = str(base / (name + '.sock'))
            if restore:
                config['SMOL_TEST_SNAPSHOT'] = str(snapshot)
            log = (args.out / (name + '.log')).open('w')
            logs.append(log)
            process = subprocess.Popen([str(args.launcher.resolve()), str(root),
                str(args.kernel.resolve()), str(args.library.resolve())], env=config,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            owned.append(process)
            return process, Path(config['SMOL_TEST_CONTROL'])

        def wait_for(predicate, process, timeout=30):
            end = time.monotonic() + timeout
            while not predicate():
                if process.poll() is not None:
                    raise RuntimeError('VM exited early: ' + str(process.returncode))
                if time.monotonic() >= end:
                    raise TimeoutError('VM readiness deadline')
                time.sleep(0.02)

        def command(path, message, expect_ok=True):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(50)
                stream.connect(str(path))
                stream.sendall((message + '\n').encode())
                reply = bytearray()
                while not reply.endswith(b'\n'):
                    part = stream.recv(4096)
                    if not part:
                        break
                    reply.extend(part)
                text = reply.decode()
                assert text.startswith('OK ' if expect_ok else 'ERR '), text
                return text.strip()

        def exchange(address, request=b"?"):
            ip, port = address.rsplit(':', 1)
            with socket.create_connection((ip.strip('[]'), int(port)), timeout=3) as stream:
                stream.sendall(request)
                reply = bytearray()
                while len(reply) < 8:
                    part = stream.recv(8 - len(reply))
                    if not part:
                        raise RuntimeError('early EOF from restored server')
                    reply.extend(part)
                return int.from_bytes(reply, 'little')

        def stop(process):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        source, control = start('source')
        wait_for(lambda: (root / 'listener-ready').exists() and control.exists(), source)
        addresses = (root / 'listener-ready').read_text().splitlines()
        assert len(addresses) == 2
        assert [exchange(address) for address in addresses] == [1, 2]
        original_generation = exchange(addresses[1], b'G')
        quiet = (1 << 64) - 1
        assert original_generation != quiet
        assert exchange(addresses[1], b'G') == quiet
        command(control, 'CHECKPOINT listeners')
        result['checks'].append('checkpointed original listeners and counter=2')
        command(control, 'RESUME')
        wrong_state = command(control, 'RESTORE listeners', expect_ok=False)
        assert 'must be paused' in wrong_state, wrong_state
        result['checks'].append('restore while running rejects without consuming checkpoint')
        assert exchange(addresses[1]) == 3
        extra_port = exchange(addresses[1], b'N')
        extra = '127.0.0.1:' + str(extra_port)
        assert exchange(extra) == 4
        created_generation = exchange(addresses[1], b'G')
        assert original_generation < created_generation < quiet
        assert exchange(addresses[0], b'D') == 1
        removed_generation = exchange(addresses[1], b'G')
        assert created_generation < removed_generation < quiet
        try:
            exchange(addresses[0])
            raise AssertionError('removed original listener still accepts')
        except ConnectionRefusedError:
            pass
        result['checks'].append('closed original IPv4 listener and opened a new port after checkpoint')
        command(control, 'PAUSE')
        ip, port = addresses[0].rsplit(':', 1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind((ip, int(port)))
            occupied.listen(1)
            rejected = command(control, 'RESTORE listeners', expect_ok=False)
            assert 'restore listener' in rejected, rejected
            resume_rejected = command(control, 'RESUME', expect_ok=False)
            assert 'restore is incomplete' in resume_rejected, resume_rejected
            capture_rejected = command(control, 'CHECKPOINT partial', expect_ok=False)
            assert 'restore is incomplete' in capture_rejected, capture_rejected
        result['checks'].append('port conflict rejects restore and prevents resuming partial state')
        result['restore_reply'] = command(control, 'RESTORE listeners')
        result['checks'].append('same retained checkpoint retries successfully after conflict is removed')
        command(control, 'RESUME')
        assert exchange(addresses[0]) == 3
        assert exchange(addresses[1]) == 4
        assert exchange(addresses[1], b'G') == quiet, 'restored watcher cursor changed during ordinary traffic'
        restored_generation = exchange(addresses[1], b'R')
        assert restored_generation == original_generation, (restored_generation, original_generation)
        result['checks'].append('reopened watcher reports the restored lower generation while existing cursor stays quiet')
        new_port = exchange(addresses[1], b'N')
        assert new_port != int(addresses[0].rsplit(':', 1)[1])
        assert original_generation < exchange(addresses[1], b'G') < quiet
        new_address = '127.0.0.1:' + str(new_port)
        assert exchange(new_address, b'D') == 1
        assert original_generation < exchange(addresses[1], b'G') < quiet
        assert exchange(addresses[1], b'G') == quiet
        result['checks'].append('restored watcher receives new listen and close changes without idle notifications')
        try:
            exchange(extra)
            raise AssertionError('post-checkpoint listener survived rewind')
        except ConnectionRefusedError:
            pass
        result['checks'].append('rewind restores original IPv4/IPv6 endpoints and RAM and closes later listener')
        consumed = command(control, 'RESTORE listeners', expect_ok=False)
        assert 'ENOENT' in consumed, consumed
        stop(source)
        content = (args.out / 'source.log').read_text()
        loaded = [line.removeprefix('SMOL_RUNTIME_LIBRARY ') for line in content.splitlines()
                  if line.startswith('SMOL_RUNTIME_LIBRARY ')]
        assert len(loaded) == 1 and Path(loaded[0]).resolve() == args.library.resolve()
        result['passed'] = True
    except Exception as error:
        result['error'] = repr(error)
    finally:
        for process in owned:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()
        temporary.cleanup()
        (args.out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)
    raise SystemExit(not result['passed'])


if __name__ == '__main__':
    main()
