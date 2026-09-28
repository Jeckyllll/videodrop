#!/usr/bin/env python3
"""Build a redistributable ZIP from an explicit program allowlist, never user data."""
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import updater


def build(root=ROOT, output=None):
    root = Path(root).resolve()
    output = Path(output or root / 'dist' / updater.ASSET)
    config = updater.read_json(root / 'version.json')
    updater.version_tuple(config['version'])
    files = updater.program_files(root)
    if not updater.REQUIRED <= set(files):
        raise ValueError('Missing program files')
    hashes = {name: updater.sha256(root / name) for name in files}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in files:
            bundle.write(root / name, name)
        bundle.writestr('release-manifest.json', json.dumps({'version': config['version'], 'files': hashes}, indent=2))
    output.with_suffix('.zip.sha256').write_text(updater.sha256(output) + '  ' + output.name + '\n')
    return output


if __name__ == '__main__':
    path = build()
    print(f'{path}\n{path.stat().st_size} bytes; program files only')
