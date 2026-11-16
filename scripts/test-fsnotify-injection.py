#!/usr/bin/env python3
"""Run as root in the guest to check injected inotify names and mask routing."""

import ctypes
import os
from pathlib import Path
import select
import struct
import tempfile
import time


MODIFY = 0x2
ATTRIB = 0x4
CREATE = 0x100
DELETE = 0x200
ISDIR = 0x40000000
MASK = MODIFY | ATTRIB | CREATE | DELETE
libc = ctypes.CDLL(None, use_errno=True)
libc.inotify_init1.argtypes = [ctypes.c_int]
libc.inotify_init1.restype = ctypes.c_int
libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
libc.inotify_add_watch.restype = ctypes.c_int


def checked(value):
    if value < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return value


def watch(fd, path):
    return checked(libc.inotify_add_watch(fd, os.fsencode(path), MASK))


def events(fd):
    result = []
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
            break
        data = os.read(fd, 65536)
        offset = 0
        while offset < len(data):
            wd, mask, cookie, length = struct.unpack_from("iIII", data, offset)
            offset += 16
            name = data[offset:offset + length].split(b"\0", 1)[0]
            offset += length
            result.append((wd, mask, os.fsdecode(name)))
    return result


def inject(path, mask):
    # One request per write, as the guest agent does; shell printf may split it.
    fd = os.open('/proc/smolvm-fsnotify', os.O_WRONLY)
    try:
        request = f"{mask:x} {path}\n".encode()
        assert os.write(fd, request) == len(request)
    finally:
        os.close(fd)


def verify(fd, path, mask, expected):
    assert not events(fd), "unexpected events before injection"
    inject(path, mask)
    received = events(fd)
    assert sorted(received) == sorted(expected), (hex(mask), path, expected, received)


def main():
    with tempfile.TemporaryDirectory(prefix="smolvm-inotify-") as temporary:
        root = Path(temporary)
        file = root / 'changed.txt'
        file.write_text('original')
        directory = root / 'nested'
        directory.mkdir()
        fd = checked(libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC))
        try:
            parent = watch(fd, root)
            child = watch(fd, file)
            nested = watch(fd, directory)
            for mask in [MODIFY, ATTRIB, MODIFY | ATTRIB]:
                verify(fd, file, mask, [(parent, mask, file.name), (child, mask, '')])
            for mask in [CREATE, DELETE]:
                verify(fd, file, mask, [(parent, mask, file.name)])
            # Recovery may combine create and inode changes in one request.
            verify(fd, file, CREATE | MODIFY | ATTRIB, [
                (parent, CREATE, file.name),
                (parent, MODIFY | ATTRIB, file.name),
                (child, MODIFY | ATTRIB, ''),
            ])
            verify(fd, directory, ATTRIB | ISDIR, [
                (parent, ATTRIB | ISDIR, directory.name),
                (nested, ATTRIB | ISDIR, ''),
            ])
        finally:
            os.close(fd)
    print('PASS: modification/attribute child names, file watches, dirent and combined masks')


if __name__ == '__main__':
    main()
