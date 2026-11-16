"""Inventory a live documents tree for a combined restic snapshot."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path


EXCLUDED_NAMES = ('node_modules', '.venv', '__pycache__', '.pytest_cache',
                  '.mypy_cache', '.ruff_cache', '.cache', '.npm', '.pnpm-store', '.DS_Store')


def absolute_tree(value):
    if not isinstance(value, str) or '\0' in value:
        raise ValueError('Invalid documents tree path')
    path = Path(value)
    if (path.anchor != '/' or path.as_posix() != value or len(path.parts) == 1
            or '..' in path.parts or path.parts[1] == 'bundle'):
        raise ValueError('Invalid documents tree path')
    return path


def escape_pattern(path):
    return re.sub(r'([\\*?\[\]])', r'\\\1', str(path))


def inventory(entries):
    h = hashlib.sha256()
    files = total_bytes = 0
    for path, kind, size in sorted(entries):
        h.update(json.dumps((path, kind, size), separators=(',', ':')).encode('ascii') + b'\n')
        if kind == 'file':
            files += 1
            total_bytes += size
    return {'files': files, 'bytes': total_bytes, 'inventory_sha256': h.hexdigest()}


def scan(root, exclude=None):
    """With an exclusion list, inventory the live source and add its restic filters."""
    entries = []

    def visit(directory, relative):
        with os.scandir(directory) as iterator:
            children = list(iterator)
        if exclude is not None and any(child.name == '.git' for child in children):
            if directory == root:
                raise ValueError('The documents source is a Git working tree')
            exclude.append(escape_pattern(directory))
            return
        entries.append((relative.as_posix(), 'dir', 0))
        for child in children:
            if exclude is not None and child.name in EXCLUDED_NAMES:
                continue
            path = relative / child.name
            info = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                visit(Path(child.path), path)
            elif stat.S_ISREG(info.st_mode):
                entries.append((path.as_posix(), 'file', info.st_size))
            elif stat.S_ISLNK(info.st_mode):
                entries.append((path.as_posix(), 'symlink', 0))
            elif not stat.S_ISSOCK(info.st_mode):
                raise ValueError('Unsupported special file in documents tree')

    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError('Documents source must be a directory')
    if exclude is not None:
        exclude.extend(f'{escape_pattern(root)}/**/{name}' for name in EXCLUDED_NAMES)
    visit(root, Path('.'))
    return inventory(entries)


def snapshot_entries(stream, root):
    for line in stream:
        node = json.loads(line)
        if node.get('message_type', node.get('struct_type')) == 'node':
            path = Path(node['path']).relative_to(root)
            kind = node['type']
            yield path.as_posix(), kind, node['size'] if kind == 'file' else 0
