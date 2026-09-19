"""docs/tsi-udp-routing.md: exercise functions from actual ordered patches."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent

def member_patch(text):
    selected = []
    active = False
    for line in text.splitlines(True):
        if line.startswith('diff --git '):
            active = False
        if line.startswith('--- '):
            active = line.strip() == '--- a/net/tsi/af_tsi.c'
        if active:
            selected.append(line)
    return ''.join(selected)

def source_tree(destination, corrected):
    target = destination / 'net/tsi/af_tsi.c'
    target.parent.mkdir(parents=True)
    patches = sorted((ROOT / 'patches').glob('0*.patch'))
    first = next(p for p in patches if p.name.startswith('0011'))
    text = first.read_text()
    body = text.split('+++ b/net/tsi/af_tsi.c\n', 1)[1].split('diff --git ', 1)[0]
    lines = body.splitlines(True)
    target.write_text(''.join(line[1:] for line in lines if line.startswith('+')))
    for patch in patches:
        if patch.name[:4] <= '0011' or (not corrected and patch.name.startswith('0034')):
            continue
        selected = member_patch(patch.read_text())
        if selected:
            result = subprocess.run(['patch', '--fuzz=0', '-p1'], cwd=destination,
                                    input=selected, text=True, capture_output=True)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
    return target.read_text()

def function(source, name):
    marker = source.index(name + '(')
    start = source.rfind('\nstatic ', 0, marker) + 1
    opening = source.index('{', marker)
    depth = 1
    end = opening + 1
    while depth:
        if source[end] == '{':
            depth += 1
        elif source[end] == '}':
            depth -= 1
        end += 1
    return source[start:end] + '\n'

class Routing(unittest.TestCase):
    def build(self, directory, source, listener=False):
        harness = 'listener_harness.c' if listener else 'send_harness.c'
        shutil.copy(HERE / harness, directory / 'harness.c')
        if listener:
            members = {'listener-under-test.c': 'tsi_has_udp_listener'}
        else:
            members = {'sendmsg-under-test.c': 'tsi_dgram_sendmsg',
                       'poll-under-test.c': 'tsi_poll',
                       'recvmsg-original.c': 'tsi_dgram_recvmsg'}
        for filename, name in members.items():
            (directory / filename).write_text(function(source, name))
        flags = ['-Wno-sign-compare'] if listener else []
        result = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                 *flags, str(directory / 'harness.c'), '-o', str(directory / 'test')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return directory / 'test'

    def test_actual_patched_send_receive(self):
        cases = ['connected', 'sendto', 'initial', 'local', 'connected-local',
                 'unconnected-nameless', 'poll-connected', 'poll-connected-local',
                 'poll-stream', 'receive-local', 'receive-proxy', 'receive-both',
                 'poll-error-only', 'dnat-sendto', 'dnat-connected',
                 'loopback-unbound', 'loopback-init']
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            executable = self.build(directory, source_tree(directory, True))
            for case in cases:
                with self.subTest(case=case):
                    result = subprocess.run([str(executable), case], capture_output=True, text=True, timeout=3)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_actual_patched_listener(self):
        cases = ['v4-unbound', 'v4-connected', 'v6-unbound', 'v6-connected',
                 'v4-missing', 'v6-missing', 'invalid-family', 'short-address',
                 'null-address', 'v4-wildcard-connected', 'v4-wildcard-unbound',
                 'v4-wildcard-route-error', 'v4-wildcard-missing',
                 'v4-wildcard-uc-index-connected', 'v4-wildcard-alternate-source-connected']
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            executable = self.build(directory, source_tree(directory, True), listener=True)
            for case in cases:
                with self.subTest(case=case):
                    result = subprocess.run([str(executable), case], capture_output=True, text=True, timeout=3)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_predecessor_reproduces_causal_failures(self):
        for listener, cases in [(False, ['poll-connected', 'dnat-connected']),
                                (True, ['v4-connected', 'v4-wildcard-connected'])]:
            with tempfile.TemporaryDirectory() as temporary:
                directory = pathlib.Path(temporary)
                executable = self.build(directory, source_tree(directory, False), listener)
                for case in cases:
                    with self.subTest(case=case):
                        result = subprocess.run([str(executable), case], capture_output=True, text=True, timeout=3)
                        self.assertNotEqual(result.returncode, 0, 'predecessor unexpectedly passed ' + case)

    def test_complete_source_preserves_upstream_locking(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = source_tree(pathlib.Path(temporary), True)
            self.assertIn('mutex_lock(&tsk->accept_lock)', source)
            self.assertIn('mutex_init(&tsk->accept_lock)', source)
            self.assertIn('pr_warn_ratelimited(', source)

if __name__ == '__main__':
    unittest.main()
