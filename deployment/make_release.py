#!/usr/bin/env python3
"""Create a reviewable public repository package using an explicit allowlist.

Does not create a repository, upload, deploy or activate a schedule.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build import build
from deployment.public_data import runtime_data

FILES = (
    'build.py', 'README.md', 'refresh.cmd', 'UPDATE_NOW.cmd',
    'collector/collect.py', 'collector/probe_routes.py', 'collector/refresh.py', 'collector/auto_normalize.py',
    'ui/app.js', 'ui/styles.css', 'ui/template.html',
    'deployment/__init__.py', 'deployment/public_data.py', 'deployment/run_ci.py',
    'deployment/make_release.py', 'deployment/README_GITHUB.md',
    '.github/workflows/refresh-pages.yml',
    'tests/test_public_data.py', 'tests/test_workflow.py', 'tests/operations_app.test.cjs',
)
OPTIONAL = ('VERIFICATION.md', 'tests/test_public_operations.py')
DATA = ('notices', 'refresh_state', 'refresh_status', 'refresh_error', 'refresh_config')
IGNORE = '''# Raw evidence and local test runs never belong in the public repository.
evidence/
__pycache__/
*.py[cod]
*.lock
*.pending
.env
.env.*
*.pem
*.key
dist/
index.html
'''


def prepare(root, destination):
    root, destination = Path(root), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name in FILES + tuple(x for x in OPTIONAL if (root / x).exists()):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    for name in DATA:
        source = root / 'data' / (name + '.json')
        if not source.exists():
            continue
        value = json.loads(source.read_text(encoding='utf-8'))
        if name == 'notices':
            # Saved official evidence establishes hashes without pretending this
            # packaging step made any new request or reviewed attachment content.
            from collector.refresh import seed_baselines
            value = seed_baselines(root, value)
        target = destination / 'data' / (name + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(runtime_data(value), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (destination / '.gitignore').write_text(IGNORE, encoding='utf-8')
    build(destination)
    # Do not ship generated Pages files in the repository package. They are
    # produced by the workflow; the separate preview is a downloadable artifact.
    shutil.rmtree(destination / 'dist')
    (destination / 'index.html').unlink()
    manifest = [{'path': p.relative_to(destination).as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(destination.rglob('*')) if p.is_file()]
    (destination / 'PUBLIC_MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def package(root, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='public-office-release-') as temporary:
        project = Path(temporary) / 'public-office-jobs'
        manifest = prepare(root, project)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for entry in sorted(project.rglob('*')):
                if entry.is_file(): archive.write(entry, entry.relative_to(Path(temporary)).as_posix())
        with zipfile.ZipFile(output) as archive:
            if archive.testzip(): raise ValueError('ZIP integrity failure')
        return {'path': str(output), 'files': len(manifest) + 1,
                'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(ROOT, args.output), ensure_ascii=False, indent=2))
