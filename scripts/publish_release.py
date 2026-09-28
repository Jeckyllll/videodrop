#!/usr/bin/env python3
"""Publish the already committed and pushed version. Developer-only, needs gh."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from build_release import ROOT, build
sys.path.insert(0, str(ROOT))
import updater


def command(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def main():
    config = updater.read_json(ROOT / 'version.json')
    version, repository = config['version'], config['repository']
    updater.version_tuple(version)
    if command('git', 'status', '--porcelain'):
        raise ValueError('Сначала сохраните все изменения в Git и отправьте их в GitHub.')
    head = command('git', 'rev-parse', 'HEAD')
    command('gh', 'api', f'repos/{repository}/commits/{head}', '--jq', '.sha')
    visibility = command('gh', 'repo', 'view', repository, '--json', 'visibility', '--jq', '.visibility')
    if visibility != 'PUBLIC':
        raise ValueError('Для обновлений без входа пользователей репозиторий релизов должен быть публичным.')
    existing = subprocess.run(['gh', 'release', 'view', 'v'+version, '--repo', repository],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if existing.returncode == 0:
        raise ValueError('Эта версия уже выпущена. Укажите следующий номер в version.json.')
    archives = [build(edition=edition) for edition in updater.ASSETS]
    with tempfile.TemporaryDirectory() as folder:
        notes = Path(folder) / 'notes.md'
        notes.write_text(f'''VideoDrop {version} для macOS.

**Для сотрудников:** [VideoDrop Lite](https://github.com/{repository}/releases/download/v{version}/VideoDrop-Lite-mac.zip) — скачивание, выбор качества/формата, открытие файлов и автообновление. Без отправки в облако, аккаунтов VideoDrop и доступа к данным владельца.

**Полная версия:** VideoDrop-mac.zip — скачивание и отправка в YouTube через Chrome.

Распакуйте выбранный ZIP в постоянную папку и откройте «Запустить VideoDrop.command».
Нужен Python 3.10+; для YouTube — Node.js 22+ или Deno. Первое подключение расширения Chrome выполняется вручную.

Полная версия и Lite получают обновления своей редакции; одна не заменяет другую.
Настройки, история, входы и видео пользователей в пакет не включены.
После изменения расширения может потребоваться нажать ↻ в chrome://extensions.

Изменения программы: https://github.com/{repository}/commits/v{version}
''')
        assets = [str(path) for archive in archives for path in (archive, archive.with_suffix('.zip.sha256'))]
        subprocess.run(['gh', 'release', 'create', 'v'+version, *assets,
                        '--repo', repository, '--target', head, '--title', 'VideoDrop '+version,
                        '--notes-file', str(notes), '--draft'], cwd=ROOT, check=True)
        subprocess.run(['gh', 'release', 'edit', 'v'+version, '--repo', repository,
                        '--draft=false', '--latest'], cwd=ROOT, check=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
