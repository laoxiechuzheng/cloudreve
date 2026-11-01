#!/usr/bin/env python3
"""Package the pinned frontend and compiled binaries without runtime build tools."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def revision(path):
    return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()


def pack_assets():
    build = ROOT / 'assets' / 'build'
    version = json.loads((build / 'version.json').read_text(encoding='utf-8'))
    if version != {'version': '3.8.3', 'name': 'cloudreve-frontend'}:
        raise ValueError('Frontend version must remain cloudreve-frontend 3.8.3')
    for name in ('index.html', 'service-worker.js', 'share-ui/share.js', 'share-ui/share.css'):
        if not (build / name).is_file():
            raise ValueError('Missing built asset: ' + name)
    with zipfile.ZipFile(ROOT / 'assets.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(build.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(ROOT).as_posix())


def pack_releases():
    commit = revision(ROOT)
    ref = os.environ.get('GITHUB_REF_NAME', '')
    version = ref[1:] if re.fullmatch(r'v3\.8\.3-custom\.[A-Za-z0-9.-]+', ref) else '3.8.3-custom.dev-' + commit[:8]
    common = {
        'version': version,
        'backend_version': '3.8.3',
        'asset_version': '3.8.3',
        'required_db_version': '3.8.1',
        'backend_commit': commit,
        'frontend_commit': revision(ROOT / 'assets'),
        'go_version': subprocess.check_output(['go', 'version'], text=True).strip(),
        'repository': os.environ.get('GITHUB_REPOSITORY', 'laoxiechuzheng/cloudreve'),
    }
    checksums = []
    for arch in ('amd64', 'arm64'):
        binary = ROOT / 'dist' / arch / 'cloudreve'
        manifest = dict(common, platform='linux/' + arch, binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
        output = ROOT / 'dist' / ('cloudreve_' + version + '_linux_' + arch + '.tar.gz')
        with tarfile.open(output, 'w:gz') as archive:
            info = archive.gettarinfo(str(binary), 'cloudreve')
            info.mode = 0o755
            with binary.open('rb') as source:
                archive.addfile(info, source)
            payload = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
            info = tarfile.TarInfo('build-info.json')
            info.size, info.mode = len(payload), 0o644
            archive.addfile(info, io.BytesIO(payload))
            for name in ('LICENSE', 'README.md', 'deploy'):
                archive.add(ROOT / name, arcname=name, filter=lambda member: None if '__pycache__' in member.name or member.name.endswith('.pyc') else member)
        checksums.append(hashlib.sha256(output.read_bytes()).hexdigest() + '  ' + output.name)
    (ROOT / 'dist' / 'SHA256SUMS').write_text('\n'.join(checksums) + '\n', encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in ('assets', 'release'):
        raise SystemExit('Usage: package-release.py assets|release')
    (pack_assets if sys.argv[1] == 'assets' else pack_releases)()
