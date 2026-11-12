#!/usr/bin/env python3
"""Manage a loopback-only PostgreSQL 17 validation instance without root."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / '.runtime' / 'pgsql'
BIN = PREFIX / 'usr' / 'lib' / 'postgresql' / '17' / 'bin'
DATA = ROOT / '.runtime' / 'postgres-data'
SOCKET = ROOT / '.runtime' / 'pg-socket'
LOG = ROOT / '.runtime' / 'logs' / 'postgres.log'
PORT = '55432'


def run(name: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment['LD_LIBRARY_PATH'] = str(PREFIX / 'usr' / 'lib' / 'x86_64-linux-gnu')
    return subprocess.run([str(BIN / name), *args], env=environment, check=check)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['start', 'stop', 'status'])
    args = parser.parse_args()
    if not (BIN / 'postgres').exists():
        raise SystemExit('Run python3 scripts/fetch_postgres.py first')
    if args.command == 'status':
        raise SystemExit(run('pg_ctl', '-D', str(DATA), 'status', check=False).returncode)
    if args.command == 'stop':
        run('pg_ctl', '-D', str(DATA), '-m', 'fast', '-w', 'stop', check=False)
        return
    SOCKET.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if not (DATA / 'PG_VERSION').exists():
        run('initdb', '-D', str(DATA), '-U', 'h2', '--auth-local=trust',
            '--auth-host=trust', '--locale=C.UTF-8', '-E', 'UTF8')
    if run('pg_ctl', '-D', str(DATA), 'status', check=False).returncode != 0:
        run('pg_ctl', '-D', str(DATA), '-l', str(LOG), '-w', '-o',
            f'-h 127.0.0.1 -p {PORT} -k {shlex.quote(str(SOCKET))}', 'start')
    for database in ['h2_validation', 'h2_test']:
        # Database names are fixed constants; no user-supplied SQL is interpolated.
        probe = subprocess.run(
            [str(BIN / 'psql'), '-h', '127.0.0.1', '-p', PORT, '-U', 'h2',
             '-d', 'postgres', '-tAc', f"SELECT 1 FROM pg_database WHERE datname='{database}'"],
            env={**os.environ, 'LD_LIBRARY_PATH': str(PREFIX / 'usr/lib/x86_64-linux-gnu')},
            capture_output=True, text=True, check=True,
        )
        if probe.stdout.strip() != '1':
            run('createdb', '-h', '127.0.0.1', '-p', PORT, '-U', 'h2', database)
    run('psql', '-h', '127.0.0.1', '-p', PORT, '-U', 'h2', '-d', 'h2_validation',
        '-c', 'SELECT version();')
    print(f'Validation database: postgresql+psycopg://h2@127.0.0.1:{PORT}/h2_validation')
    print('Local validation only: trust authentication, loopback binding.')


if __name__ == '__main__':
    main()
