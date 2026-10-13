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
import unittest

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
        self.assertIn('QRXFER_SCRIPT version=%s grid=yes sha256=%s\n' % (
            qrxfer.__version__, context['qrxfer_sha256']), run.stdout)
        self.assertTrue(run.stdout.endswith('QRXFER_READY %s\n' % remote))
        self.assertEqual(sorted(os.listdir(str(remote))), ['transfer.py'])  # only the helper is typed in
        for name in ('send', 'send_qr'):
            command = shlex.split((local / (name + '.txt')).read_text())
            self.assertEqual(command[:3], ['python3', str(installed), 'send'])
        self.assertEqual(installed.read_bytes(), (PROJECT / 'qrxfer.py').read_bytes())

        # A version from before the grid: reported, and send_qr.txt needs no --codec.
        installed.write_text('__version__ = "1.0.0"\nprint("qr only")\n')
        context, local, remote, run = self.prepare_with_script(str(installed))
        self.assertIn('QRXFER_SCRIPT version=1.0.0 grid=no sha256=', run.stdout)
        self.assertNotIn('--codec', (local / 'send_qr.txt').read_text())
        self.assertTrue((local / 'send_qr.txt').read_text().endswith(' --fps 20 --terminal'))

        context, local, remote, run = self.prepare_with_script(str(self.root / 'missing'))
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn('QRXFER_READY', run.stdout)
        self.assertFalse(remote.exists())
        with self.assertRaisesRegex(ValueError, 'absolute'):
            self.prepare_with_script('bin/qrxfer')


if __name__ == '__main__':
    unittest.main()
