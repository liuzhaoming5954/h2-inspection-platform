#!/usr/bin/env python3
"""Fetch official PostgreSQL 17 Ubuntu packages into a project-local directory.

No apt sources or system installation are changed. SHA256 is verified against
the package index fetched over HTTPS. Run as the regular desktop user.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / '.runtime' / 'pg-packages'
PREFIX = ROOT / '.runtime' / 'pgsql'
BASE = 'https://apt.postgresql.org/pub/repos/apt/'


def dist_codename() -> str:
    for line in Path('/etc/os-release').read_text().splitlines():
        if line.startswith('VERSION_CODENAME='):
            return line.split('=', 1)[1].strip()
    raise SystemExit('Could not determine distribution codename from /etc/os-release')


def download(url: str, destination: Path) -> None:
    partial = destination.with_suffix(destination.suffix + '.part')
    subprocess.run(
        ['curl', '--fail', '--location', '--retry', '3', '--retry-all-errors',
         '--continue-at', '-', '--connect-timeout', '20', '--max-time', '1200', '--silent', '--show-error',
         url, '-o', str(partial)], check=True,
    )
    partial.replace(destination)


def main() -> None:
    PACKAGES.mkdir(parents=True, exist_ok=True)
    index = PACKAGES / 'Packages.gz'
    if not index.exists():
        download(
            BASE + f'dists/{dist_codename()}-pgdg/main/binary-amd64/Packages.gz', index
        )
    entries: dict[str, dict[str, str]] = {}
    for paragraph in gzip.decompress(index.read_bytes()).decode().split('\n\n'):
        fields = {}
        for line in paragraph.splitlines():
            if ': ' in line and not line.startswith(' '):
                key, value = line.split(': ', 1)
                fields[key] = value
        name = fields.get('Package')
        if name not in {'postgresql-17', 'postgresql-client-17', 'libpq5'}:
            continue
        previous = entries.get(name)
        if previous and subprocess.run(
            ['dpkg', '--compare-versions', fields['Version'], 'le', previous['Version']]
        ).returncode == 0:
            continue
        entries[name] = fields
    if set(entries) != {'postgresql-17', 'postgresql-client-17', 'libpq5'}:
        raise SystemExit('Official package index did not include required packages')
    manifest = []
    for name, entry in entries.items():
        package = PACKAGES / Path(entry['Filename']).name
        if not package.exists():
            print(f"Downloading {name} {entry['Version']}", flush=True)
            download(BASE + entry['Filename'], package)
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        if digest != entry['SHA256']:
            raise SystemExit(f'SHA256 mismatch: {package.name}; remove file and retry')
        subprocess.run(['dpkg-deb', '-x', str(package), str(PREFIX)], check=True)
        manifest.append({'package': name, 'version': entry['Version'], 'sha256': digest})
    (PREFIX / 'package-manifest.json').write_text(json.dumps(manifest, indent=2))
    print(f'PostgreSQL extracted to {PREFIX}')


if __name__ == '__main__':
    main()
