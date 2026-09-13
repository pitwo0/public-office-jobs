"""Separate public presentation data from durable collector state.

Only public recruitment facts and official source URLs belong in Pages. Raw
downloads, paths, complete run logs and previous record versions stay local.
Runtime files retain content hashes and queue state for the next Actions run.
"""
import copy
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

PRIVATE_KEYS = {
    'rawPath', 'sourcePath', 'path', 'batch', 'body_file', 'headers_file', 'artifacts',
    'previousVerified', 'previousVerifiedUnit', 'previousAttachments',
    'retiredUnits', 'attachmentReviewReceipt', 'scopedReviewReceipt',
    'lastRunSummary', 'responseHeaders', 'requestHeaders', 'traceback',
}
PRIVATE_KEY = re.compile(r'(?:secret|password|authorization|api[_-]?key|access[_-]?token)', re.I)
LOCAL_PATH = re.compile(r'(?:/workspace/|/tmp/|/home/|(?<![A-Za-z])[A-Za-z]:[\\/]|evidence/)[^\s\"<>]*')


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()
                if k not in PRIVATE_KEYS and not k.startswith('previous_') and not PRIVATE_KEY.search(k)}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, str):
        if value.startswith(('http://', 'https://')):
            parsed = urlsplit(value)
            if parsed.username or parsed.password or any(PRIVATE_KEY.search(k) for k in parse_qs(parsed.query)):
                return ''
        return LOCAL_PATH.sub('[비공개 경로]', value)
    return value


def runtime_data(value):
    """Sanitize committed JSON, preserving IDs, hashes, polling state and facts."""
    return _clean(copy.deepcopy(value))


def public_data(data, generated_at=None):
    """Return presentation schema only; never publish the collector inbox."""
    from build import validate_data
    validate_data(data)
    result = runtime_data({k: data[k] for k in ('schemaVersion', 'asOfDate', 'notices', 'refresh') if k in data})
    # Internal fingerprints are needed by the updater, not website readers.
    for notice in result['notices']:
        for source in notice.get('sources', []):
            source.pop('sha256', None)
        for key in ('semanticHash', 'conditionsHash', 'resultsHash', 'rawSha256', 'baselineHashes'):
            notice.get('autoSync', {}).pop(key, None)
        notice.pop('collectionBaseline', None)
        for unit in notice['units']:
            unit.pop('competitionHistory', None)
    result['publishedDataUrl'] = 'data/notices.json'
    result['presentation'] = {
        'generatedAt': generated_at or data.get('presentation', {}).get('generatedAt') or dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'note': '화면 생성 시각이며 개별 원문·첨부 확인 시각이 아닙니다.',
    }
    validate_data(result)
    return result


def prepare_runtime_files(root):
    """Call before a repository commit; no raw evidence or logs are staged."""
    from build import commit_files
    root = Path(root)
    outputs = {}
    for name in ('notices', 'refresh_state', 'refresh_status', 'refresh_error'):
        path = root / 'data' / (name + '.json')
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        outputs[path] = (json.dumps(runtime_data(data), ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    commit_files(outputs)
    return list(outputs)
