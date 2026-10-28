#!/usr/bin/env python3
"""Stage a signed Chrome update; never creates a signing key or publishes it."""
import argparse
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from build_server_extension import ROOT, build


def stage(key, output, base, crx_tool=None):
    key = Path(key).expanduser().absolute()
    if key.is_symlink() or not key.is_file():
        raise ValueError('Provide an existing regular signing key; the release tool will not create one.')
    if stat.S_IMODE(key.stat().st_mode) & 0o077:
        raise ValueError('Signing key permissions must exclude group and other users (chmod 600).')
    if key.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('Keep the signing key outside the repository.')
    output = Path(output).expanduser().absolute()
    if key.resolve().is_relative_to(output.resolve()):
        raise ValueError('Signing key must not be inside the release directory.')
    parts = urlsplit(base)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or any(c in base for c in '<>"\' \r\n')):
        raise ValueError('Provide a clean HTTPS base URL.')
    base = base.rstrip('/')
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / 'extension-server/manifest.json').read_text())
    version=manifest.get('version','')
    if (not isinstance(version,str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){0,3}',version)
            or any(int(part)>65535 for part in version.split('.'))):
        raise ValueError('Invalid Chrome extension version.')
    crx, xml = output / f'VideoDrop-{version}.crx', output / 'extension-update.xml'
    if crx.exists() or xml.exists():
        raise ValueError('Output files already exist; use a new staging directory.')
    tool = Path(crx_tool or ROOT / 'scripts/node_modules/crx3/bin/crx3.js')
    if not tool.is_file():
        raise ValueError('Install release dependencies with npm ci --prefix scripts.')
    with tempfile.TemporaryDirectory(prefix='.videodrop-release-', dir=output) as temp:
        source = build(ROOT, Path(temp) / 'source.zip')
        staged = Path(temp) / 'managed.zip'
        with zipfile.ZipFile(source) as src, zipfile.ZipFile(staged, 'w', zipfile.ZIP_DEFLATED) as dst:
            for name in src.namelist():
                data = src.read(name)
                if name == 'manifest.json':
                    manifest['update_url'] = base + '/extension-update.xml'
                    data = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
                dst.writestr(name, data)
        # Generate in the private staging folder; move only after both artifacts exist.
        tmp_crx, tmp_xml = Path(temp) / crx.name, Path(temp) / 'extension-update.xml'
        with staged.open('rb') as archive:
            subprocess.run(['node', str(tool), '-p', str(key), '-o', str(tmp_crx), '-x', str(tmp_xml),
                '--appVersion=' + manifest['version'], '--browserVersion=' + manifest['minimum_chrome_version'] + '.0.0.0',
                '--crxURL=' + base + '/' + crx.name], stdin=archive, check=True, capture_output=True)
        os.rename(tmp_crx, crx)
        try:
            os.rename(tmp_xml, xml)
        except Exception:
            crx.unlink(missing_ok=True)
            raise
    return crx, xml


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', required=True, help='Existing private signing key, stored outside the repository')
    parser.add_argument('--output', required=True, help='Empty staging directory, not the live server')
    parser.add_argument('--base', required=True, help='HTTPS URL of the eventual update directory')
    args = parser.parse_args()
    for path in stage(args.key, args.output, args.base):
        print(path)
