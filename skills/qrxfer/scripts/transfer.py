#!/usr/bin/env python3
"""Deterministic helpers for an agent-operated qrxfer download over VNC (stdlib, Python 3.6+)."""

import argparse
import base64
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
import zlib


def write_json(path, value):
    with open(path, 'w') as out:
        json.dump(value, out, ensure_ascii=True, indent=2, sort_keys=True)
        out.write('\n')


def server_address(value):
    match = re.fullmatch(r'([^:\s/]+)(?:(::?)([0-9]+))?', value)
    if not match or value.startswith('-'):
        raise ValueError('Use host, host:display or host::port')
    host, separator, number = match.groups()
    port = int(number or 0) + (0 if separator == '::' else 5900)
    if not 1 <= port <= 65535:
        raise ValueError('VNC port is out of range')
    return {'host': host, 'port': port, 'viewer_server': '%s::%d' % (host, port)}


def python_command(code):
    return 'python3 -c ' + shlex.quote(code)


def prepare(args):
    address = server_address(args.server)
    if not os.path.isabs(args.source) or args.source == '/':
        raise ValueError('Provide a remote absolute path other than /')
    if args.script is not None and not (os.path.isabs(args.script) and args.script != '/'):
        raise ValueError('Provide the remote qrxfer script as an absolute path')
    project = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.realpath(__file__)))))
    work = tempfile.mkdtemp(prefix='qrxfer-download-')
    os.chmod(work, 0o700)
    remote = '/tmp/qrxfer-download-' + uuid.uuid4().hex
    local_qrxfer = os.path.join(project, 'qrxfer.py')
    # With an existing remote script only the helper has to be typed in.
    sources = [('transfer.py', os.path.realpath(__file__))]
    if args.script is None:
        sources.insert(0, ('qrxfer.py', local_qrxfer))
    payload = {}
    for name, path in sources:
        with open(path, 'rb') as source:
            content = source.read()
        payload[name] = [base64.b64encode(content).decode('ascii'),
                         hashlib.sha256(content).hexdigest()]
    encoded = base64.b64encode(zlib.compress(json.dumps(payload).encode('ascii'), 9))
    # An existing script is only read, never run, and before anything is created:
    # a wrong path fails without leaving a remote directory behind.
    bootstrap = """import base64,hashlib,json,os,re,zlib
d=%r
s=%r
if s:
    with open(s,'rb') as f:
        q=f.read()
    v=re.search(br'^__version__ = "([^"]+)"',q,re.M)
    print('QRXFER_SCRIPT version=%%s grid=%%s sha256=%%s'%%(v.group(1).decode('ascii') if v else '?',
          'yes' if b'--codec' in q else 'no',hashlib.sha256(q).hexdigest()))
p=json.loads(zlib.decompress(base64.b64decode(%r)).decode('ascii'))
os.mkdir(d,0o700)
for name,(encoded,digest) in p.items():
    data=base64.b64decode(encoded)
    assert hashlib.sha256(data).hexdigest()==digest
    with open(os.path.join(d,name),'xb') as f:
        f.write(data)
print('QRXFER_READY '+d)
""" % (remote, args.script, encoded.decode('ascii'))
    # Keep every generated shell command on one line for VNC keyboard delivery.
    bootstrap = python_command('exec(%r)' % bootstrap)
    helper = shlex.quote(remote + '/transfer.py')
    bundle = 'python3 %s bundle --source %s --output %s' % (
        helper, shlex.quote(args.source), shlex.quote(remote))
    send = 'python3 %s send %s %s --fps 20' % (
        shlex.quote(args.script or remote + '/qrxfer.py'), shlex.quote(remote + '/payload.tar.xz'),
        shlex.quote(remote + '/manifest.json'))
    # Fallback when the colour grid cannot be shown or read: QR codes in the terminal.
    # Plain --terminal also works with qrxfer versions that predate the grid.
    send_qr = send + ' --terminal'
    cleanup = python_command("import os,shutil;p=%r;assert os.path.isdir(p) "
                             "and not os.path.islink(p);shutil.rmtree(p);"
                             "print('QRXFER_CLEANUP_OK')" % remote)
    # quit.txt is the key that stops a sender (typed without Enter).
    for name, command in [('bootstrap', bootstrap), ('bundle', bundle), ('send', send),
                           ('send_qr', send_qr), ('quit', 'q'), ('cleanup', cleanup)]:
        with open(os.path.join(work, name + '.txt'), 'w') as out:
            out.write(command)
    with open(os.path.join(work, 'passphrase.txt'), 'w') as out:
        out.write(secrets.token_hex(32))
    os.chmod(os.path.join(work, 'passphrase.txt'), 0o600)
    os.mkdir(os.path.join(work, 'received'))
    context = dict(address, source=args.source, dest=os.path.abspath(os.path.expanduser(args.dest)),
                   work=work, remote_work=remote, project=project, script=args.script,
                   qrxfer_sha256=file_hash(local_qrxfer))
    write_json(os.path.join(work, 'context.json'), context)
    print(json.dumps(context, indent=2))


def check_viewer(window):
    identity = subprocess.check_output(['xprop', '-id', str(window), 'WM_CLASS'])
    if b'vnc' not in identity.lower():
        raise ValueError('The target window is not a VNC viewer')


def type_file(args):
    check_viewer(args.window)
    with open(args.file, 'rb') as source:
        content = source.read()
    if b'\n' in content or b'\r' in content:
        raise ValueError('Input files must contain one line without a newline')
    # xdotool uses XTEST only while the target window has the focus and falls back
    # to synthetic events otherwise, which VNC viewers ignore: focus it first, and
    # keys after a focus change are dropped instead of reaching another window.
    subprocess.check_call(['xdotool', 'windowfocus', '--sync', str(args.window)])
    subprocess.check_call(['xdotool', 'type', '--window', str(args.window),
                           '--clearmodifiers', '--delay', '1', '--file', args.file])
    if args.enter:
        subprocess.check_call(['xdotool', 'key', '--window', str(args.window),
                               '--clearmodifiers', 'Return'])
    print('Keys sent to VNC window %s; check the remote screen.' % args.window)


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def keep_awake(args):
    # Screensavers on both sides start counting at the last key typed through the
    # viewer and cover the grid half an hour into a long transfer. xscreensaver ignores
    # pointer warps, so it is told directly; the viewer forwards pointer motion, which
    # keeps the remote awake. No keys: the grid sender stops on q.
    check_viewer(args.window)
    while running(args.pid):
        if shutil.which('xscreensaver-command'):
            subprocess.call(['xscreensaver-command', '-deactivate'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        where = dict(line.split('=', 1) for line in subprocess.check_output(
            ['xdotool', 'getmouselocation', '--shell'], universal_newlines=True).split())
        for x, y in ((1, 1), (2, 2)):  # the remote screen's corner, in the grid's black margin
            subprocess.check_call(['xdotool', 'mousemove', '--window', str(args.window), str(x), str(y)])
        subprocess.check_call(['xdotool', 'mousemove', '--screen', where['SCREEN'], where['X'], where['Y']])
        print('%s nudged VNC window %s' % (time.strftime('%H:%M:%S'), args.window), flush=True)
        deadline = time.monotonic() + args.every
        while running(args.pid) and time.monotonic() < deadline:
            time.sleep(min(1.0, args.every))
    print('Process %d has exited; stopped.' % args.pid)


def paths_in(root):
    yield '.', root
    if os.path.isdir(root) and not os.path.islink(root):
        def walk_error(error):
            raise error
        for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            dirs.sort()
            for name in sorted(dirs + files):
                path = os.path.join(base, name)
                yield os.path.relpath(path, root), path


class HashReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size):
        data = self.source.read(size)
        self.digest.update(data)
        return data


def bundle(args):
    root = os.path.abspath(args.source.rstrip('/') or '/')
    name = os.path.basename(root)
    if not name or not os.path.lexists(root):
        raise ValueError('Source does not exist or is a filesystem root')
    # Temporary outputs must not become part of the source being enumerated.
    output = os.path.realpath(args.output)
    resolved = os.path.realpath(root)
    if os.path.isdir(root) and os.path.commonpath([output, resolved]) == resolved:
        raise ValueError('Bundle output must be outside the source')
    os.makedirs(output, exist_ok=True)
    archive_path = os.path.join(output, 'payload.tar.xz')
    manifest_path = os.path.join(output, 'manifest.json')
    if os.path.lexists(archive_path) or os.path.lexists(manifest_path):
        raise ValueError('Bundle output already exists; use a fresh workspace')
    entries = {}
    with tarfile.open(archive_path, 'w:xz', preset=6) as archive:
        for rel, path in paths_in(root):
            before = os.lstat(path)
            if not any(check(before.st_mode) for check in
                       (stat.S_ISREG, stat.S_ISDIR, stat.S_ISLNK)):
                raise ValueError('Unsupported special file: ' + path)
            member = archive.gettarinfo(path, arcname=name if rel == '.' else name + '/' + rel)
            member.uid = member.gid = 0
            member.uname = member.gname = ''
            if stat.S_ISREG(before.st_mode):
                # Preserve hard-linked names as independent regular files.
                member.type, member.linkname, member.size = tarfile.REGTYPE, '', before.st_size
                with open(path, 'rb') as source:
                    reader = HashReader(source)
                    archive.addfile(member, reader)
                after = os.lstat(path)
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('Source changed while reading: ' + path)
                item = {'type': 'file', 'size': before.st_size,
                        'sha256': reader.digest.hexdigest()}
            elif stat.S_ISDIR(before.st_mode):
                archive.addfile(member)
                item = {'type': 'directory'}
            elif stat.S_ISLNK(before.st_mode):
                archive.addfile(member)
                item = {'type': 'symlink', 'target': member.linkname}
            else:
                raise ValueError('Unsupported special file: ' + path)
            entries[rel] = item
    manifest = {'source': root, 'name': name, 'entries': entries,
                'archive_sha256': file_hash(archive_path)}
    write_json(manifest_path, manifest)
    files = [entry for entry in entries.values() if entry['type'] == 'file']
    print(json.dumps({'files': len(files), 'bytes': sum(e['size'] for e in files),
                      'archive_bytes': os.path.getsize(archive_path),
                      'manifest_sha256': file_hash(manifest_path),
                      'symlinks': {key: val['target'] for key, val in entries.items()
                                   if val['type'] == 'symlink'}}, sort_keys=True))


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for data in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def safe_parts(name):
    parts = name.split('/')
    if not name or name.startswith('/') or any(p in ('', '.', '..') for p in parts):
        raise ValueError('Unsafe archive path: ' + name)
    return parts


def verify(root, expected):
    actual = dict(paths_in(root))
    if set(actual) != set(expected):
        raise ValueError('Directory entries differ from the source manifest')
    for rel, item in expected.items():
        path = actual[rel]
        mode = os.lstat(path).st_mode
        if item['type'] == 'directory':
            valid = stat.S_ISDIR(mode)
        elif item['type'] == 'symlink':
            valid = stat.S_ISLNK(mode) and os.readlink(path) == item['target']
        elif item['type'] == 'file':
            valid = (stat.S_ISREG(mode) and os.path.getsize(path) == item['size']
                     and file_hash(path) == item['sha256'])
        else:
            valid = False
        if not valid:
            raise ValueError('Content verification failed: ' + rel)


def publish(source, dest, name):
    # Staging is inside dest, so publication cannot fail with EXDEV.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    stem, extension = os.path.splitext(name)
    index = 0
    while True:
        candidate = name if index == 0 else '%s (%d)%s' % (stem, index, extension)
        target = os.path.join(dest, candidate)
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
            return target
        code = ctypes.get_errno()
        if code not in (errno.EEXIST, errno.ENOTEMPTY):
            raise OSError(code, os.strerror(code), target)
        index += 1


def install(args):
    received = os.path.join(args.work, 'received')
    manifest_path = os.path.join(received, 'manifest.json')
    with open(manifest_path) as source:
        manifest = json.load(source)
    with open(os.path.join(args.work, 'context.json')) as source:
        context = json.load(source)
    if manifest['source'] != os.path.normpath(context['source']):
        raise ValueError('Received source differs from the requested source')
    name = manifest['name']
    if len(safe_parts(name)) != 1 or name != os.path.basename(manifest['source']):
        raise ValueError('Unexpected bundle root name')
    archive_path = os.path.join(received, 'payload.tar.xz')
    if file_hash(archive_path) != manifest['archive_sha256']:
        raise ValueError('Payload archive SHA-256 differs from its manifest')
    dest = context['dest']
    os.makedirs(dest, exist_ok=True)
    staging = tempfile.mkdtemp(prefix='.qrxfer-', dir=dest)
    try:
        with tarfile.open(archive_path, 'r:xz') as archive:
            members = archive.getmembers()
            names, links = set(), {}
            for member in members:
                parts = safe_parts(member.name)
                if parts[0] != name or member.name in names:
                    raise ValueError('Unexpected or duplicate member: ' + member.name)
                names.add(member.name)
                if member.issym():
                    links[member.name] = member.linkname
                elif not (member.isfile() or member.isdir()):
                    raise ValueError('Unsupported archive member: ' + member.name)
            for member in members:
                parts = safe_parts(member.name)
                if any('/'.join(parts[:i]) in links for i in range(1, len(parts))):
                    raise ValueError('Archive path traverses a symlink: ' + member.name)
            # Absolute and parent-relative symlink targets are preserved only
            # after all data is extracted; extraction never follows a link.
            regular = [member for member in members if not member.issym()]
            archive.extractall(staging, members=regular, filter='data')
            for path, target in links.items():
                os.symlink(target, os.path.join(staging, path))
        root = os.path.join(staging, name)
        verify(root, manifest['entries'])
        target = publish(root, dest, name)
        files = [e for e in manifest['entries'].values() if e['type'] == 'file']
        result = {'status': 'PASS', 'target': target, 'files': len(files),
                  'bytes': sum(e['size'] for e in files),
                  'manifest_sha256': file_hash(manifest_path),
                  'external_or_broken_links': [key for key, entry in manifest['entries'].items()
                      if entry['type'] == 'symlink' and
                      (os.path.isabs(entry['target']) or
                       os.path.commonpath([os.path.realpath(target),
                           os.path.realpath(os.path.join(target, key))]) != os.path.realpath(target) or
                       not os.path.exists(os.path.join(target, key)))]}
        write_json(os.path.join(args.work, 'verification.json'), result)
        secret = os.path.join(args.work, 'passphrase.txt')
        if os.path.exists(secret):
            os.unlink(secret)
        print(json.dumps(result, indent=2))
    finally:
        shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command')
    p = commands.add_parser('prepare', help='prepare command files; does not connect')
    p.add_argument('--server', required=True)
    p.add_argument('--source', required=True)
    p.add_argument('--dest', required=True)
    p.add_argument('--script', help='qrxfer already on the remote (absolute path); '
                   'only the helper is installed then')
    p.set_defaults(run=prepare)
    p = commands.add_parser('type', help='type one file into a specific VNC window')
    p.add_argument('--window', type=int, required=True)
    p.add_argument('--file', required=True)
    p.add_argument('--enter', action='store_true')
    p.set_defaults(run=type_file)
    p = commands.add_parser('keepawake', help='keep both screensavers off until a process exits')
    p.add_argument('--window', type=int, required=True)
    p.add_argument('--pid', type=int, required=True, help='the local receiver')
    p.add_argument('--every', type=float, default=240.0, help='seconds between nudges')
    p.set_defaults(run=keep_awake)
    p = commands.add_parser('bundle', help='run on the sender; pack source and hash manifest')
    p.add_argument('--source', required=True)
    p.add_argument('--output', required=True)
    p.set_defaults(run=bundle)
    p = commands.add_parser('install', help='verify and publish a received bundle')
    p.add_argument('--work', required=True)
    p.set_defaults(run=install)
    args = parser.parse_args()
    if not hasattr(args, 'run'):
        parser.error('choose a subcommand')
    try:
        args.run(args)
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        parser.exit(1, 'error: %s\n' % error)


if __name__ == '__main__':
    main()
