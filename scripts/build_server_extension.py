#!/usr/bin/env python3
"""Build only the server Chrome client. No local config, credentials or legacy drivers."""
from pathlib import Path
import json
import zipfile

ROOT=Path(__file__).resolve().parents[1]
FILES=('manifest.json','popup.html','popup.css','popup.js','bridge.js','background.js','README.md')


def build(root=ROOT,output=None):
    root=Path(root)
    output=Path(output or root/'server/web/VideoDrop-Chrome.zip')
    output.parent.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((root/'extension-server/manifest.json').read_text())
    if any(item in manifest['permissions'] for item in ('debugger','nativeMessaging')):
        raise ValueError('Legacy local permissions must not be distributed')
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as package:
        for name in FILES:
            package.write(root/'extension-server'/name,name)
    return output


if __name__=='__main__':
    print(build())
