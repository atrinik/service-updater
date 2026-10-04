#!/usr/bin/env python3
"""Create an allowlisted, reproducible MIT source deployment bundle."""
import gzip
import hashlib
import io
from pathlib import Path
import re
import sys
import tarfile


def main():
    version = sys.argv[1]
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise SystemExit('stable version required')
    root = Path(__file__).resolve().parents[1]
    files = [root / name for name in ('service_updater.py', 'core.py', 'LICENSE', 'README.md')]
    files += sorted((root / 'config').glob('*.json')) + sorted((root / 'docs').glob('*.md'))
    files += sorted((root / 'adapters').glob('*.py'))
    output = root / 'dist'
    output.mkdir(exist_ok=True)
    archive = output / ('atrinik-service-updater-' + version + '.tar.gz')
    with archive.open('wb') as stream, gzip.GzipFile(filename='', fileobj=stream, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as bundle:
            for path in files:
                if path.is_symlink() or not path.is_file():
                    raise SystemExit('invalid package input')
                content = path.read_bytes()
                info = tarfile.TarInfo(str(path.relative_to(root)))
                info.size, info.mode = len(content), 0o644
                bundle.addfile(info, io.BytesIO(content))
            content = (version + '\n').encode()
            info = tarfile.TarInfo('VERSION')
            info.size, info.mode = len(content), 0o644
            bundle.addfile(info, io.BytesIO(content))
    (archive.with_name(archive.name + '.sha256')).write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
    print(archive.name)


if __name__ == '__main__':
    main()
