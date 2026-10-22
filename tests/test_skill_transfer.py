"""Offline behavioral tests for the VNC transfer skill's bundle/install helpers."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

import qrxfer

PROJECT = pathlib.Path(__file__).resolve().parents[1]
HELPER = PROJECT / 'skills/qrxfer/scripts/transfer.py'
SPEC = importlib.util.spec_from_file_location('skill_transfer', HELPER)
transfer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)


class SkillTransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qrxfer-skill-test-')
        self.root = pathlib.Path(self.temp.name)
        self.source = self.root / 'source with spaces'
        self.source.mkdir()
        (self.source / 'empty').mkdir()
        (self.source / '.hidden').write_bytes(b'\x00\x01\xff' * 1000)
        (self.source / 'unicode-中文.txt').write_text('nonzero test data\n')
        (self.source / 'outside').symlink_to('/not-present/qrxfer-reference.pdf')
        (self.source / 'relative').symlink_to('../external-target')
        self.output = self.root / 'bundle'
        self.work = self.root / 'local'
        self.work.mkdir()
        self.dest = self.root / 'destination'
        transfer.write_json(str(self.work / 'context.json'),
                            {'source': str(self.source), 'dest': str(self.dest)})

    def tearDown(self):
        self.temp.cleanup()

    def receive(self):
        with contextlib.redirect_stdout(io.StringIO()):
            transfer.bundle(argparse.Namespace(source=str(self.source), output=str(self.output)))
        # Exercise the actual qrxfer outer archive and authenticated container.
        packed, _, _ = qrxfer.pack_paths([str(self.output / 'payload.tar.xz'),
                                         str(self.output / 'manifest.json')])
        encrypted = qrxfer.encrypt(packed, 'offline fixture passphrase')
        qrxfer.unpack_archive(qrxfer.decrypt(encrypted, 'offline fixture passphrase'),
                              str(self.work / 'received'))

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            transfer.install(argparse.Namespace(work=str(self.work)))
        return json.loads((self.work / 'verification.json').read_text())

    def test_roundtrip_links_empty_dirs_and_existing_destination(self):
        self.receive()
        existing = self.dest / self.source.name
        existing.mkdir(parents=True)
        (existing / 'keep.txt').write_text('untouched')
        result = self.install()
        target = pathlib.Path(result['target'])
        self.assertNotEqual(target, existing)
        self.assertEqual((existing / 'keep.txt').read_text(), 'untouched')
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['files'], 2)
        self.assertTrue((target / 'empty').is_dir())
        self.assertEqual((target / '.hidden').read_bytes(), (self.source / '.hidden').read_bytes())
        self.assertEqual(os.readlink(target / 'outside'), '/not-present/qrxfer-reference.pdf')
        self.assertEqual(os.readlink(target / 'relative'), '../external-target')
        self.assertEqual(set(result['external_or_broken_links']), {'outside', 'relative'})

    def test_bundle_compresses_in_independent_xz_streams(self):
        with mock.patch.object(transfer, 'XZ_CHUNK', 1024):
            self.receive()
        payload = (self.work / 'received/payload.tar.xz').read_bytes()
        self.assertGreater(payload.count(b'\xfd7zXZ\x00'), 1)
        result = self.install()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual((pathlib.Path(result['target']) / '.hidden').read_bytes(),
                         (self.source / '.hidden').read_bytes())

    def test_payload_corruption_does_not_publish(self):
        self.receive()
        path = self.work / 'received/payload.tar.xz'
        path.write_bytes(path.read_bytes() + b'corrupt')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.install()
        self.assertFalse((self.dest / self.source.name).exists())

    def test_single_file_download(self):
        self.source = self.source / 'unicode-中文.txt'
        transfer.write_json(str(self.work / 'context.json'),
                            {'source': str(self.source), 'dest': str(self.dest)})
        self.receive()
        result = self.install()
        self.assertEqual(result['files'], 1)
        self.assertEqual(pathlib.Path(result['target']).read_bytes(), self.source.read_bytes())

    def test_tar_traversal_symlink_ancestor_and_duplicate_rejected(self):
        self.receive()
        for attack in ('traversal', 'symlink', 'duplicate'):
            with self.subTest(attack=attack):
                archive_path = self.work / 'received/payload.tar.xz'
                with tarfile.open(archive_path, 'w:xz') as archive:
                    root = tarfile.TarInfo(self.source.name)
                    root.type = tarfile.DIRTYPE
                    archive.addfile(root)
                    if attack == 'traversal':
                        archive.addfile(tarfile.TarInfo(self.source.name + '/../escaped'))
                    elif attack == 'symlink':
                        link = tarfile.TarInfo(self.source.name + '/jump')
                        link.type, link.linkname = tarfile.SYMTYPE, str(self.root)
                        archive.addfile(link)
                        archive.addfile(tarfile.TarInfo(self.source.name + '/jump/escaped'))
                    else:
                        archive.addfile(root)
                manifest_path = self.work / 'received/manifest.json'
                manifest = json.loads(manifest_path.read_text())
                manifest['archive_sha256'] = transfer.file_hash(str(archive_path))
                transfer.write_json(str(manifest_path), manifest)
                with self.assertRaises(ValueError):
                    self.install()
                self.assertFalse((self.root / 'escaped').exists())
                self.assertFalse((self.dest / self.source.name).exists())

    def test_hardlinks_are_preserved_as_files_and_fifo_is_rejected(self):
        os.link(self.source / '.hidden', self.source / 'hardlink')
        self.receive()
        self.assertEqual(self.install()['files'], 3)
        os.mkfifo(self.source / 'fifo')
        with self.assertRaisesRegex(ValueError, 'special file'):
            transfer.bundle(argparse.Namespace(source=str(self.source),
                                                output=str(self.root / 'fifo-output')))

    def test_prepare_bootstrap_and_address_semantics(self):
        self.assertEqual(transfer.server_address('192.0.2.1:69')['port'], 5969)
        self.assertEqual(transfer.server_address('192.0.2.1::5969')['port'], 5969)
        self.assertEqual(transfer.server_address('host')['port'], 5900)
        for value in ('-option', 'host::0', 'host:60000', 'host/path', 'host:69;echo'):
            with self.assertRaises(ValueError):
                transfer.server_address(value)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            transfer.prepare(argparse.Namespace(server='192.0.2.1:69', source=str(self.source),
                                                 dest=str(self.dest), script=None))
        context = json.loads(captured.getvalue())
        local, remote = pathlib.Path(context['work']), pathlib.Path(context['remote_work'])
        try:
            secret = (local / 'passphrase.txt').read_text()
            self.assertEqual((local / 'passphrase.txt').stat().st_mode & 0o777, 0o600)
            for path in [local / 'context.json'] + list(local.glob('*.txt')):
                if path.name != 'passphrase.txt':
                    self.assertNotIn(secret, path.read_text())
            subprocess.run(['sh', '-c', (local / 'bootstrap.txt').read_text()],
                            check=True, stdout=subprocess.PIPE)
            self.assertEqual((remote / 'qrxfer.py').read_bytes(), (PROJECT / 'qrxfer.py').read_bytes())
            self.assertEqual((remote / 'transfer.py').read_bytes(), HELPER.read_bytes())
            failed = subprocess.run(['sh', '-c', (local / 'bootstrap.txt').read_text()],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(failed.returncode, 0)
        finally:
            shutil.rmtree(str(local))
            if remote.exists():
                shutil.rmtree(str(remote))

    def prepare_with_script(self, script):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            transfer.prepare(argparse.Namespace(server='192.0.2.1', source=str(self.source),
                                                 dest=str(self.dest), script=script))
        context = json.loads(captured.getvalue())
        local, remote = pathlib.Path(context['work']), pathlib.Path(context['remote_work'])
        self.addCleanup(shutil.rmtree, str(local))
        self.addCleanup(shutil.rmtree, str(remote), True)
        run = subprocess.run(['sh', '-c', (local / 'bootstrap.txt').read_text()],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return context, local, remote, run

    def test_prepare_with_an_existing_remote_script(self):
        installed = self.root / 'bin dir/qrxfer'
        installed.parent.mkdir()
        shutil.copy(str(PROJECT / 'qrxfer.py'), str(installed))
        context, local, remote, run = self.prepare_with_script(str(installed))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(context['script'], str(installed))
        self.assertEqual(context['qrxfer_sha256'], transfer.file_hash(str(PROJECT / 'qrxfer.py')))
        self.assertIn('QRXFER_SCRIPT version=%s sha256=%s\n' % (
            qrxfer.__version__, context['qrxfer_sha256']), run.stdout)
        self.assertTrue(run.stdout.endswith('QRXFER_READY %s\n' % remote))
        self.assertEqual(sorted(os.listdir(str(remote))), ['transfer.py'])  # only the helper is typed in
        for name in ('send', 'send_qr'):
            command = shlex.split((local / (name + '.txt')).read_text())
            self.assertEqual(command[:3], ['python3', str(installed), 'send'])
        self.assertEqual(installed.read_bytes(), (PROJECT / 'qrxfer.py').read_bytes())

        # Another version is reported with its own digest, so the agent can tell it apart.
        installed.write_text('__version__ = "1.0.0"\nprint("old")\n')
        context, local, remote, run = self.prepare_with_script(str(installed))
        self.assertIn('QRXFER_SCRIPT version=1.0.0 sha256=%s\n' % transfer.file_hash(str(installed)), run.stdout)
        self.assertNotEqual(transfer.file_hash(str(installed)), context['qrxfer_sha256'])

        context, local, remote, run = self.prepare_with_script(str(self.root / 'missing'))
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn('QRXFER_READY', run.stdout)
        self.assertFalse(remote.exists())
        with self.assertRaisesRegex(ValueError, 'absolute'):
            self.prepare_with_script('bin/qrxfer')

    def test_keepawake_moves_the_pointer_without_keys_until_the_receiver_exits(self):
        calls = []

        def output(command, **kwargs):
            calls.append(command)
            if command[0] == 'xprop':
                return b'WM_CLASS(STRING) = "vncviewer", "Vncviewer"\n'
            return 'X=661\nY=1714\nSCREEN=0\nWINDOW=123\n'

        receiver = subprocess.Popen(['sleep', '1.2'])
        threading.Thread(target=receiver.wait).start()  # reaped, so its PID disappears
        with mock.patch.object(transfer.subprocess, 'check_output', output), \
                mock.patch.object(transfer.subprocess, 'check_call', calls.append), \
                mock.patch.object(transfer.subprocess, 'call', lambda command, **kwargs: calls.append(command)), \
                mock.patch.object(transfer.shutil, 'which', lambda name: '/usr/bin/' + name), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            started = time.monotonic()
            transfer.keep_awake(argparse.Namespace(window=42, pid=receiver.pid, every=0.4))
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn('has exited', log.getvalue())
        rounds = [i for i, command in enumerate(calls) if command[0] == 'xscreensaver-command']
        self.assertGreaterEqual(len(rounds), 2)
        self.assertEqual(calls[rounds[0]:rounds[1]], [
            ['xscreensaver-command', '-deactivate'],
            ['xdotool', 'getmouselocation', '--shell'],
            ['xdotool', 'mousemove', '--window', '42', '1', '1'],
            ['xdotool', 'mousemove', '--window', '42', '2', '2'],
            ['xdotool', 'mousemove', '--screen', '0', '661', '1714']])
        for command in calls:
            self.assertFalse({'key', 'type', 'click'} & set(command), command)
        with self.assertRaisesRegex(ValueError, 'not a VNC viewer'), \
                mock.patch.object(transfer.subprocess, 'check_output', lambda command: b'"xterm"'):
            transfer.keep_awake(argparse.Namespace(window=42, pid=os.getpid(), every=0.4))

    def test_place_moves_the_viewer_clear_of_docks_and_restores_it(self):
        atoms, calls = {}, []

        class FakeX11:
            def __getattr__(self, name):
                def call(*args):
                    if name == 'XInternAtom':
                        return atoms.setdefault(args[1].decode(), 100 + len(atoms))
                    if name == 'XSendEvent':
                        event = args[4]._obj
                        calls.append(('message', event.message_type, event.window, list(event.data)))
                    elif name == 'XChangeProperty':
                        calls.append(('hints', args[1], list(args[6])))
                    return 1
                setattr(self, name, call)  # prototypes get assigned to it
                return call

        def output(command, **kwargs):
            if command[:2] == ['xprop', '-id']:
                return b'WM_CLASS(STRING) = "vncviewer", "Vncviewer"\n'
            if command[:2] == ['xprop', '-root']:
                return '_NET_WORKAREA(CARDINAL) = 0, 643, 4000, 1917, 0, 643, 4000, 1917\n'
            return ('  Absolute upper-left X:  1440\n  Absolute upper-left Y:  643\n'
                    '  Width: 2560\n  Height: 1351\n')

        with mock.patch.object(transfer.ctypes, 'CDLL', lambda name: FakeX11()), \
                mock.patch.object(transfer.subprocess, 'check_output', output), \
                mock.patch.object(transfer.time, 'sleep', lambda s: None), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            transfer.place(argparse.Namespace(window=42, x=None, y=None, restore=False))
            transfer.place(argparse.Namespace(window=42, x=None, y=None, restore=True))
        state, vert, horz = (atoms['_NET_WM_STATE'], atoms['_NET_WM_STATE_MAXIMIZED_VERT'],
                             atoms['_NET_WM_STATE_MAXIMIZED_HORZ'])
        self.assertIn('_MOTIF_WM_HINTS', atoms)
        self.assertEqual(calls, [
            ('hints', 42, [2, 0, 0, 0, 0]),  # no decorations, so the title bar needs no room
            ('message', state, 42, [0, vert, horz, 2, 0]),  # unmaximize
            ('message', atoms['_NET_MOVERESIZE_WINDOW'], 42, [10 | 3 << 8 | 2 << 12, 1440, 643, 0, 0]),
            ('hints', 42, [2, 0, 1, 0, 0]),
            ('message', state, 42, [1, vert, horz, 2, 0])])
        self.assertEqual(json.loads(log.getvalue().splitlines()[0]),
                         {'window': 42, 'x': 1440, 'y': 643, 'width': 2560, 'height': 1351})

    def test_type_releases_return_when_the_window_closes_on_it(self):
        line = self.root / 'line.txt'
        line.write_text('secret')
        calls = []

        def check_call(command):
            calls.append(command)
            if 'Return' in command:  # the dialog closed on the key press: BadWindow
                raise subprocess.CalledProcessError(1, command)

        with mock.patch.object(transfer.subprocess, 'check_output', lambda command: b'"TigerVNC Viewer"'), \
                mock.patch.object(transfer.subprocess, 'check_call', check_call), \
                mock.patch.object(transfer.subprocess, 'call', calls.append), \
                self.assertRaises(subprocess.CalledProcessError):
            transfer.type_file(argparse.Namespace(window=42, file=str(line), enter=True))
        self.assertEqual([command[:2] for command in calls], [
            ['xdotool', 'windowfocus'], ['xdotool', 'type'], ['xdotool', 'key'], ['xdotool', 'keyup']])
        self.assertEqual(calls[-1], ['xdotool', 'keyup', 'Return'])


if __name__ == '__main__':
    unittest.main()
