#!/usr/bin/env python3
"""Build a redistributable ZIP from an explicit program allowlist, never user data."""
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import updater


def build(root=ROOT, output=None, edition='full'):
    root = Path(root).resolve()
    asset = updater.ASSETS[edition]
    output = Path(output or root / 'dist' / asset)
    config = updater.read_json(root / 'version.json')
    updater.version_tuple(config['version'])
    files = updater.program_files(root)
    if edition == 'lite':
        files = [name for name in files if name not in updater.UPLOAD_FILES]
    if not updater.required_files(edition) <= set(files):
        raise ValueError('Missing program files')
    contents = {name: (root / name).read_bytes() for name in files}
    config.update(edition=edition, asset=asset, port=8766 if edition == 'lite' else 8765)
    contents['version.json'] = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode()
    if edition == 'lite':
        # Build-time removal: upload scripts/styles and their UI containers are absent from Lite.
        contents['extension/edition.js'] = b"const VIDEODROP_EDITION = 'lite';\n"
        for name in ('web/index.html', 'extension/popup.html'):
            text = contents[name].decode()
            for fragment in ('<link rel="stylesheet" href="/youtube-ui.css">', '<link rel="stylesheet" href="youtube-ui.css">',
                             '<script src="/youtube-ui.js" defer></script>', '<script src="youtube-ui.js"></script>',
                             '<div id="youtube-settings"></div>'):
                text = text.replace(fragment, '')
            text = text.replace('VideoDrop', 'VideoDrop Lite')
            text = text.replace('На Mac и в вашем YouTube · доступ по ссылке', 'Сохранение на ваш компьютер')
            text = text.replace('Эту страницу можно закрыть. Вкладку YouTube Studio оставьте открытой.',
                                'Эту страницу можно закрыть. Скачивание продолжится.')
            if 'youtube-ui' in text or 'youtube-settings' in text or 'YouTube Studio' in text:
                raise ValueError('Upload UI must not appear in Lite')
            contents[name] = text.encode()
        manifest = json.loads(contents['extension/manifest.json'])
        manifest.update(name='VideoDrop Lite — скачать видео',
                        description='Скачать видео с открытой страницы на компьютер. Версия без отправки в облако.',
                        host_permissions=['http://127.0.0.1:8766/*'])
        manifest['permissions'] = [p for p in manifest['permissions'] if p not in ('debugger', 'alarms')]
        manifest['action']['default_title'] = 'Скачать через VideoDrop Lite'
        manifest.pop('externally_connectable', None)
        contents['extension/manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
        contents['README.md'] = (root / 'README-lite.md').read_bytes()
    import hashlib
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name, data in contents.items():
            item = zipfile.ZipInfo(name)
            item.external_attr = (0o100755 if name.endswith('.command') else 0o100644) << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(item, data)
        bundle.writestr('release-manifest.json', json.dumps({'version': config['version'], 'edition': edition, 'files': hashes}, indent=2))
    output.with_suffix('.zip.sha256').write_text(updater.sha256(output) + '  ' + output.name + '\n')
    return output


if __name__ == '__main__':
    for edition in updater.ASSETS:
        path = build(edition=edition)
        print(f'{path}\n{path.stat().st_size} bytes; {edition}; program files only')
