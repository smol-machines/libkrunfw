#!/usr/bin/env python3
"""Save and restart a real guest with two blocked TCP listeners and RAM state."""
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

        def command(path, message):
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
                assert text.startswith('OK '), text
                return text.strip()

        def exchange(address):
            ip, port = address.rsplit(':', 1)
            with socket.create_connection((ip.strip('[]'), int(port)), timeout=3) as stream:
                stream.sendall(b'?')
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
        time.sleep(0.1)  # Both server threads return to blocked accept.
        result['save_reply'] = command(control, 'SAVE ' + str(snapshot))
        result['checks'].append('saved two waiting listeners and counter=2')
        checkpoint_file = snapshot / 'checkpoint.bin'
        checkpoint = checkpoint_file.read_bytes()
        marker = b'"host_network_abi":8'
        assert checkpoint.count(marker) == 1
        try:
            checkpoint_file.write_bytes(checkpoint.replace(marker, b'"host_network_abi":7'))
            incompatible, _ = start('incompatible', True)
            incompatible.wait(timeout=30)
            assert incompatible.returncode != 0
            assert 'checkpoint networking mode or ABI does not match' in (args.out / 'incompatible.log').read_text()
        finally:
            checkpoint_file.write_bytes(checkpoint)
        result['checks'].append('incompatible recorded networking ABI rejected')
        conflict, _ = start('conflict', True)
        conflict.wait(timeout=30)
        assert conflict.returncode != 0, 'restored over occupied source ports'
        conflict_log = (args.out / 'conflict.log').read_text()
        assert 'restore listener' in conflict_log and ('in use' in conflict_log or 'os error 48' in conflict_log), conflict_log[-4000:]
        result['checks'].append('occupied port rejects fresh VM restore')
        command(control, 'RESUME')
        assert exchange(addresses[0]) == 3
        result['checks'].append('failed clone leaves original server usable')
        stop(source)
        restored, restored_control = start('restored', True)
        wait_for(restored_control.exists, restored)
        # Startup creates the control endpoint before restored devices resume.
        end = time.monotonic() + 15
        while True:
            try:
                value = exchange(addresses[0])
                break
            except ConnectionRefusedError:
                if restored.poll() is not None or time.monotonic() >= end:
                    raise
                time.sleep(0.02)
        assert value == 3, ('RAM counter was not restored', value)
        assert exchange(addresses[1]) == 4
        assert exchange(addresses[0]) == 5
        result['checks'].append('fresh process restores both exact addresses and RAM counter')
        stop(restored)
        for name in ['source', 'incompatible', 'conflict', 'restored']:
            content = (args.out / (name + '.log')).read_text()
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
