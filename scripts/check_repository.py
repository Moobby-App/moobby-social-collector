"""Check staged/tracked Git blobs for private artifacts and common credential patterns."""
import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys


TOKEN_PATTERNS = [
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{25,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{25,}\b'),
    re.compile(r'\bsb_secret_[A-Za-z0-9_-]{15,}\b'),
    re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
]
DATABASE_URI = re.compile(r'postgres(?:ql)?://[^:\s/]+:([^@\s]+)@', re.I)
PLACEHOLDERS = {'[YOUR-PASSWORD]', '[your-password]', '[PASSWORD]', '<password>', '${PASSWORD}', '{password}'}


def forbidden_path(name):
    path = PurePosixPath(name)
    parts = path.parts
    base = path.name.lower()
    if any(part in ('.venv', 'venv', '.auth', '__pycache__', 'chrome-profile', 'firefox-profile', 'node_modules', '.browsers', 'tweets-data') for part in parts):
        return True
    if parts and parts[0] == 'data':
        return True
    if (base == '.env' or base.startswith('.env.')) and base != '.env.example':
        return True
    if base.endswith(('.pem', '.key', '.har')) or base in ('.pgpass', '.pg_service.conf', '.ds_store'):
        return True
    if base.endswith('.json') and ('session' in base or 'cookies' in base):
        return True
    return base.startswith('debug_') and base.endswith(('.png', '.html'))


def has_credential(text):
    if any(pattern.search(text) for pattern in TOKEN_PATTERNS):
        return True
    return any(match.group(1) not in PLACEHOLDERS for match in DATABASE_URI.finditer(text))


def git(*args):
    return subprocess.run(['git', *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--staged', action='store_true')
    group.add_argument('--tracked', action='store_true')
    args = parser.parse_args()
    try:
        if args.staged:
            paths = git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z').decode().split('\0')
        else:
            paths = git('ls-files', '-z').decode().split('\0')
        errors = []
        count = 0
        for name in filter(None, paths):
            count += 1
            if forbidden_path(name):
                errors.append((name, 'private/generated file must not be tracked'))
                continue
            # Inspect the INDEX blob, not an untracked local .env/session.
            raw = git('show', ':' + name)
            if b'\x00' in raw:
                continue
            if has_credential(raw.decode('utf-8', errors='replace')):
                errors.append((name, 'possible credential; matched value is not printed'))
    except (subprocess.CalledProcessError, UnicodeError):
        print('Unable to inspect Git index. Run from the repository folder.')
        return 1
    if errors:
        for name, reason in errors:
            print('BLOCKED: {} ({})'.format(name, reason))
        return 1
    print('Repository check passed: {} files; no blocked artifacts or common credential patterns.'.format(count))
    return 0


if __name__ == '__main__':
    sys.exit(main())
