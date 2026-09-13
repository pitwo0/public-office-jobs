#!/usr/bin/env python3
"""Bounded collection, recoverable state, build and explicit Git persistence.

Uses only the Python standard library. This program does not enable a schedule,
create a repository or deploy anything by itself.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PERSISTED_FILES = (
    'data/notices.json', 'data/refresh_state.json',
    'data/refresh_status.json', 'data/refresh_error.json',
)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def fatal_record(root, before, before_state, started, exc):
    """A fatal exception is a failed attempt, never an empty successful crawl."""
    from build import commit_files
    data = copy.deepcopy(before)
    state = copy.deepcopy(before_state)
    # Preserve a detected access stop even if a later evidence/build write failed.
    try:
        blocked = read(root / 'data/refresh_state.json').get('originBlocked')
        if blocked:
            state['originBlocked'] = blocked
    except (OSError, ValueError, TypeError):
        pass
    report = {
        'status': 'failed', 'mode': 'live', 'startedAt': started,
        'finishedAt': utc(), 'noticeCount': len(data['notices']),
        'added': 0, 'changedIds': [], 'records': [],
        'newRemoteRequests': None,
        'errors': [{'phase': 'fatal_collector', 'type': type(exc).__name__,
                    'error': '자동 수집 처리 중 오류. 이전 정상 자료를 보존했습니다. Actions 실행 기록을 확인하세요.',
                    'existingNoticePreserved': True}],
    }
    previous = data.get('refresh', {})
    data['refresh'] = dict(previous, status='failed', mode='live',
                           lastAttemptAt=started,
                           lastSuccessAt=previous.get('lastSuccessAt'),
                           lastRunSummary=report)
    commit_files({
        root / 'data/notices.json': encoded(data),
        root / 'data/refresh_state.json': encoded(state),
        root / 'data/refresh_status.json': encoded(report),
        root / 'data/refresh_error.json': encoded(report),
    })
    return report


def emit_outputs(values):
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with Path(path).open('a', encoding='utf-8', newline='\n') as handle:
            for key, value in values.items():
                handle.write(f'{key}={value}\n')


def prepare(root=ROOT, collect=False, collector=None, builder=None, sanitizer=None):
    """A failed collection can publish its status; failed build cannot deploy."""
    from build import build, validate_data
    from collector.refresh import run
    from deployment.public_data import prepare_runtime_files

    root = Path(root)
    collector = collector or run
    builder = builder or build
    sanitizer = sanitizer or prepare_runtime_files
    before = read(root / 'data/notices.json')
    validate_data(before)
    state_path = root / 'data/refresh_state.json'
    before_state = read(state_path) if state_path.exists() else {
        'inbox': {}, 'candidates': {}, 'nextPage': 2,
    }
    started = utc()
    report = {'status': 'skipped', 'reason': 'COLLECTION_ENABLED가 true가 아닙니다.'}
    if collect:
        try:
            report = collector(root=root)
            if report.get('status') not in ('success', 'partial_success', 'failed'):
                raise ValueError('Unsupported collection status')
            # A previous fatal marker must not remain current after recovery.
            (root / 'data/refresh_error.json').unlink(missing_ok=True)
        except Exception as exc:
            traceback.print_exc()
            report = fatal_record(root, before, before_state, started, exc)
    # Runtime JSON persists in the repo; raw responses and private paths do not.
    sanitizer(root)
    builder(root)
    if not (root / 'dist/index.html').is_file():
        raise RuntimeError('Build did not produce dist/index.html')
    if not (root / 'dist/data/notices.json').is_file():
        raise RuntimeError('Build did not produce dist/data/notices.json')
    result = {'collection_status': report['status'], 'build_ready': 'true'}
    emit_outputs(result)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a', encoding='utf-8') as handle:
            handle.write('### 공공기관 사무직 갱신\n\n')
            handle.write(f'- 수집 결과: `{report["status"]}`\n')
            handle.write('- 공개 화면 생성 완료. 저장 커밋·Pages 배포 성공은 다음 단계를 확인하세요.\n')
            if report['status'] == 'skipped':
                handle.write('- 수집 비활성: 기존 저장 데이터로 화면만 생성했습니다.\n')
    print(json.dumps(result, ensure_ascii=False))
    return result


def git(root, *arguments):
    return subprocess.run(['git', '-C', str(root), *arguments], check=True,
                          text=True, encoding='utf-8', capture_output=True).stdout.strip()


def persist(root=ROOT, branch=None):
    """Only explicit state files are committed; a conflicting push fails closed."""
    root = Path(root)
    branch = branch or os.environ.get('GITHUB_REF_NAME')
    if not branch:
        raise ValueError('A branch name is required for persistence')
    git(root, 'check-ref-format', '--branch', branch)
    if git(root, 'diff', '--cached', '--name-only'):
        raise RuntimeError('Refusing to commit a repository with pre-staged changes')
    paths = [name for name in PERSISTED_FILES
             if (root / name).exists() or git(root, 'ls-files', '--', name)]
    if not paths:
        raise RuntimeError('No persistent data files found')
    git(root, 'add', '--all', '--', *paths)
    staged = git(root, 'diff', '--cached', '--name-only').splitlines()
    if not set(staged) <= set(PERSISTED_FILES):
        raise RuntimeError('Unexpected file staged for public persistence')
    if staged:
        git(root, '-c', 'user.name=github-actions[bot]',
            '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'commit', '-m', 'Update official recruitment data and collection status')
    # Also verifies remote access when this run has no data changes. No force,
    # rebase, merge or retry: concurrent human changes require a fresh run.
    git(root, 'push', 'origin', f'HEAD:refs/heads/{branch}')
    result = {'persisted': 'true', 'commit': git(root, 'rev-parse', 'HEAD'),
              'changed': 'true' if staged else 'false'}
    emit_outputs(result)
    print(json.dumps(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--collect', choices=('true', 'false'), default='false')
    parser.add_argument('--persist', action='store_true')
    parser.add_argument('--branch')
    args = parser.parse_args()
    try:
        if args.persist:
            persist(args.root, args.branch)
        else:
            prepare(args.root, args.collect == 'true')
        return 0
    except Exception:
        emit_outputs({'build_ready': 'false', 'persisted': 'false'})
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
