#!/usr/bin/env python3
"""GPT-free, bounded official collection -> unit records -> JSON -> existing HTML.

No AI API, reviewed-rule import, attachments download, scheduler, or deployment.
No automatic retries/redirects. Unclear units are queued/held, never invented.
"""
import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import re
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import ROOT, screen_outputs, commit_files, validate_data
from collector import probe_routes as transport
from collector.auto_normalize import (normalize, semantic_hash, conditions_hash, competition_record,
                                      text_key, date_value, integer, FIELDS, OFFICE)

DEFAULT = {'listPages': 2, 'detailLimit': 4, 'newNoticeLimit': 2, 'lookbackDays': 60,
           'requestLimit': 6, 'responseByteLimit': 2_000_000, 'totalByteLimit': 8_000_000,
           'intervalSeconds': 1, 'workType': 'R1040', 'ncs': 'R600002'}
KST = dt.timezone(dt.timedelta(hours=9))


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    commit_files({path: encoded(value)})


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def config_for(root):
    path = root / 'data/refresh_config.json'
    config = dict(DEFAULT, **(load(path) if path.exists() else {}))
    if not 1 <= config['listPages'] <= 2 or not 1 <= config['detailLimit'] <= 4:
        raise ValueError('목록은 최대2페이지, 상세는 최대4건/회')
    if not 1 <= config['newNoticeLimit'] <= config['detailLimit']:
        raise ValueError('새 공고 반영 한도 오류')
    if config['requestLimit'] != 6 or config['lookbackDays'] not in range(1, 61):
        raise ValueError('요청6회·최근60일 이내 한도')
    if config['workType'] != 'R1040' or config['ncs'] != 'R600002':
        raise ValueError('검증된 JOB-ALIO 비정규직·경영회계사무 필터만 지원')
    if not 1 <= config['intervalSeconds'] <= 10:
        raise ValueError('요청 간격은1~10초')
    if not 1024 <= config['responseByteLimit'] <= 2_000_000 or not 1024 <= config['totalByteLimit'] <= 8_000_000:
        raise ValueError('응답당2MB·회차8MB 상한')
    return config


@contextlib.contextmanager
def single_writer(root):
    """OS releases lock on process exit; a leftover file is not a stale lock."""
    path = root / 'data/refresh.lock'; path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as file:
        file.seek(0)
        if not file.read(1): file.write(b'0'); file.flush()
        file.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            try: msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: raise RuntimeError('다른 갱신이 실행 중입니다.')
            try: yield
            finally: file.seek(0); msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try: fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('다른 갱신이 실행 중입니다.')
            try: yield
            finally: fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def accepted(raw, event):
    if event.get('status') != 'received' or event.get('truncated') or event.get('stop_origin'):
        raise ValueError('원문 수신 실패·불완전·접근제한: ' + event.get('status', 'unknown'))
    if 'html' not in (event.get('content_type') or '').lower():
        raise ValueError('HTML 응답 아님')
    if hashlib.sha256(raw).hexdigest() != event['sha256']:
        raise ValueError('수신 원문 해시 불일치')


def parse_current_detail(raw, url, expected):
    detail = transport.parse_detail(raw, url)
    if detail['idx'] != str(expected['idx']) or detail['org'] != expected['org']:
        raise ValueError('공고 ID·기관 대응 불일치')
    # Title edits are legitimate changes. A stale queue title cannot reject them.
    return detail


def row_signature(row):
    closing = re.search(r'(?:20)?\d{2}[.]\d{2}[.]\d{2}', row.get('closing_raw', ''))
    return tuple(row.get(k) for k in ('id', 'title', 'org', 'region_raw', 'employment_raw')) + (closing[0] if closing else None,)


def select_details(rows, data, state, config, as_of=None):
    known = {n['id'] for n in data['notices']}
    # Retain unfinished discoveries even when they leave the newest list pages.
    inbox = copy.deepcopy(state.get('inbox', {}))
    for row in rows:
        entry = inbox.setdefault(row['id'], {})
        if row_signature(entry.get('row', {})) != row_signature(row): entry.pop('lastAttemptAt', None)
        entry['row'] = row
    day = as_of or data['asOfDate']; saved = {n['id']: n for n in data['notices']}
    today = dt.date.fromisoformat(day)
    def category(entry):
        row = entry['row']; n = saved.get(row['id'])
        closing = re.search(r'(?:20)?\d{2}[.]\d{2}[.]\d{2}', row.get('closing_raw', ''))
        deadline = date_value(closing[0]) if closing else n.get('deadline') if n else None
        if n is None:
            # Discoveries already saved before closing remain work to finish;
            # they may first be published directly on the past-notices page.
            return 'new'
        if not deadline or deadline >= day: return 'active'
        elapsed = (today - dt.date.fromisoformat(deadline)).days
        def results_unknown(unit):
            comp = unit.get('competition')
            stages = (comp or {}).get('stages', [])
            check = n.get('autoSync', {}).get('resultCheck', {})
            uncertain_units = set(check.get('unmatchedUnitIds', [])) | {
                issue.get('unitId') for issue in check.get('issues', [])}
            return (not comp or not stages or stages[-1].get('selected') is None or
                    stages[-1].get('applicants') is None or
                    unit['id'] in uncertain_units or
                    unit.get('competitionRecord', {}).get('status') in ('unverified', 'extraction_failed'))
        unknown = any(results_unknown(u) for u in n['units'])
        # All saved closed notices remain eligible. Fresh/unknown results receive
        # daily checks; old complete results rotate monthly. Failed attempts use
        # the same cadence, so a broken notice cannot monopolize every run.
        days = 1 if elapsed <= 30 or unknown else 30
        last = entry.get('lastAttemptAt') or entry.get('lastSuccessAt')
        last_day = dt.datetime.fromisoformat(last).astimezone(KST).date() if last else None
        if last_day and (today - last_day).days < days: return None
        return 'recent_closed' if elapsed <= 30 or unknown else 'older_closed'
    groups = {k: [] for k in ('new', 'active', 'recent_closed', 'older_closed')}
    for entry in inbox.values():
        if not entry.get('row'): continue
        group = category(entry)
        if group: groups[group].append(entry['row'])
    def key(row):
        entry = inbox.get(row['id'], {})
        return (entry.get('lastAttemptAt', ''), 0 if OFFICE.search(row['title']) else 1, -int(row['idx']))
    for group in groups.values(): group.sort(key=key)
    # One slot per class prevents new jobs from starving closed-result checks.
    # Any unused slots are filled in this same priority order, oldest attempt first.
    selected = []
    order = ('new', 'active', 'recent_closed', 'older_closed')
    for name in order:
        if groups[name] and len(selected) < config['detailLimit']:
            selected.append(groups[name].pop(0))
    for row in sorted((r for group in groups.values() for r in group), key=key):
        if len(selected) >= config['detailLimit']: break
        selected.append(row)
    return selected


def page_navigation(raw, current, previous_next=2):
    """Use the official last-page link where available, never the first block cap."""
    tree = transport.Tree(raw).root
    final = []
    for link in tree.all('a'):
        if 'end' not in link.attrs.get('class', '').split(): continue
        match = re.search(r'goPage\((\d+)\)', link.attrs.get('onclick', ''))
        if match: final.append(int(match[1]))
    links = sorted(set(int(x) for x in re.findall(r'goPage\((\d+)\)', raw.decode('utf-8'))))
    terminal = max(final) if final else None
    if terminal is not None:
        successor = current + 1 if current < terminal else 2
    else:
        forward = [x for x in links if x > current]
        successor = current + 1 if current + 1 in forward else min(forward) if forward else 2
    return {'lastPage': terminal, 'nextPage': successor, 'observedPages': links}


def capture(root, config, data, state, started, recorder_class=transport.Recorder):
    if state.get('originBlocked'):
        raise RuntimeError('이전 공식 접근제한으로 자동 요청 중지. 기록을 검토하기 전에는 재요청하지 않습니다.')
    run_id = started.replace(':', '').replace('+', '_') + '-' + uuid.uuid4().hex[:8]
    folder = root / 'evidence/automatic_refresh_20260913/live' / run_id
    previous = (transport.MAX_REQUESTS, transport.MAX_BYTES_PER_RESPONSE, transport.MAX_TOTAL_BYTES)
    transport.MAX_REQUESTS = config['requestLimit']
    transport.MAX_BYTES_PER_RESPONSE = config['responseByteLimit']
    transport.MAX_TOTAL_BYTES = config['totalByteLimit']
    rec = recorder_class(folder)
    batch = {'schemaVersion': 'automatic-capture-0.1', 'startedAt': started, 'items': [],
             'config': config, 'automaticRetries': 0, 'redirects': False, 'errors': [],
             'initialNoticeIds': [n['id'] for n in data['notices']]}
    today = dt.datetime.fromisoformat(started).astimezone(KST).date()
    form = {'work_type': config['workType'], 'detail_code': config['ncs'], 'ing': '2',
            's_date': (today - dt.timedelta(days=config['lookbackDays'])).strftime('%Y.%m.%d'),
            'e_date': today.strftime('%Y.%m.%d'), 'order': 'REG_DATE', 'sort': 'DESC'}
    rows = []; navigation = {}; next_page = state.get('nextPage', 2)
    def request(url, label, kind, **more):
        if rec.events: time.sleep(config['intervalSeconds'])
        raw, event = rec.fetch(url, label)
        ref = {'path': (folder / event['body_file']).relative_to(root).as_posix(), 'url': url,
               'sha256': event['sha256'], 'receivedAt': event['finished_at']}
        item = {'kind': kind, 'ref': ref, 'event': copy.deepcopy(event), **more}
        batch['items'].append(item)
        save(folder / 'batch.json', batch)
        accepted(raw, event)
        return raw, item
    try:
        for slot in range(config['listPages']):
            page = 1 if slot == 0 else max(2, int(next_page))
            if slot and navigation.get('lastPage') == 1: break
            if slot and navigation.get('lastPage') and page > navigation['lastPage']: page = 2
            if slot and not navigation.get('lastPage') and not navigation.get('observedPages'): break
            url = 'https://job.alio.go.kr/recruit.do?' + urlencode(dict(form, pageNo=page))
            try:
                raw, item = request(url, 'list_' + str(page), 'list', page=page)
                parsed = transport.parse_list(raw, url, page)
                rows.extend(parsed['rows'])
                navigation = page_navigation(raw, page)
                if slot or config['listPages'] == 1: next_page = navigation['nextPage']
                batch['lastPage'] = page
                item['validated'] = True
            except Exception as exc:
                batch['errors'].append({'phase': 'list', 'page': page, 'error': str(exc)})
                if slot == 0: break
            if any(e.get('stop_origin') for e in rec.events): break
        if not any(e.get('stop_origin') for e in rec.events):
            # Previously saved detail URLs stay usable when a list request fails.
            for row in select_details(rows, data, state, config, today.isoformat()):
                try:
                    raw, item = request(row['source_url'], 'detail_' + row['idx'], 'detail', expected=row)
                    parse_current_detail(raw, row['source_url'], row)
                    item['validated'] = True
                except Exception as exc:
                    batch['errors'].append({'phase': 'detail', 'id': row['id'], 'error': str(exc)})
                if any(e.get('stop_origin') for e in rec.events): break
        batch['finishedAt'] = utc()
        batch['actualRequests'] = len(rec.events)
        batch['nextPage'] = next_page
        batch['pagination'] = navigation
        save(folder / 'batch.json', batch)
        save(folder / 'execution_config.json', dict(config, timeoutSeconds=20, automaticRetries=0, redirects=False))
        return batch, folder / 'batch.json'
    except Exception as exc:
        batch['errors'].append({'phase': 'capture', 'error': str(exc)})
        batch.update(finishedAt=utc(), actualRequests=len(rec.events))
        save(folder / 'batch.json', batch)
        return batch, folder / 'batch.json'
    finally:
        transport.MAX_REQUESTS, transport.MAX_BYTES_PER_RESPONSE, transport.MAX_TOTAL_BYTES = previous


def source_data(root, ref):
    path = (root / ref['path']).resolve()
    if not path.is_relative_to(root.resolve()): raise ValueError('프로젝트 밖 원문 경로')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref['sha256']: raise ValueError('원문 해시 변경')
    return raw


def baseline_hash(root, old):
    if old.get('autoSync', {}).get('semanticHash'):
        return old['autoSync']['semanticHash']
    detail = baseline_detail(root, old)
    return semantic_hash(detail) if detail else None


def baseline_detail(root, old):
    """Optional migration from old evidence; ongoing operation needs only hashes."""
    for source in reversed(old.get('sources', [])):
        if 'job.alio.go.kr/recruitview.do' not in source.get('url', '') or not source.get('rawPath'): continue
        try:
            ref = {'path': source['rawPath'], 'sha256': source['sha256']}
            detail = transport.parse_detail(source_data(root, ref), source['url'],
                {'idx': str(old['idx']), 'org': old['organization'], 'title': old['title']})
            return detail
        except (KeyError, ValueError, OSError): continue
    return None


def seed_baselines(root, data):
    """Populate comparison hashes before private raw evidence is left out of a repo.

    Pure copy: no writes, no network, no review/fetch timestamps are advanced.
    """
    result = copy.deepcopy(data)
    for notice in result['notices']:
        previous = baseline_detail(root, notice)
        if previous:
            sync = notice.setdefault('autoSync', {})
            sync.setdefault('semanticHash', semantic_hash(previous))
            sync.setdefault('conditionsHash', conditions_hash(previous))
            sync.setdefault('baselineStatus', 'stored_evidence_hash')
    return result


def baseline_conditions_hash(root, old):
    if old.get('autoSync', {}).get('conditionsHash'):
        return old['autoSync']['conditionsHash']
    detail = baseline_detail(root, old)
    return conditions_hash(detail) if detail else None


def result_payload(unit):
    record = unit.get('competitionRecord', {})
    competition = unit.get('competition')
    ratio = competition.get('publishedRaw', competition.get('published')) if competition else None
    try: ratio = Decimal(str(ratio)) if ratio is not None else None
    except InvalidOperation: pass
    return {'ratio': ratio,
            'stages': competition.get('stages', record.get('stages', [])) if competition else record.get('stages', []),
            'status': record.get('status', 'confirmed' if competition else 'unverified')}


def merge_results(notice, old, detail, ref):
    """Exact unit matching only; malformed/missing results cannot erase good data."""
    tables = {}
    for table in detail.get('unit_results_raw', []):
        tables.setdefault(text_key(table['unit_name_raw']), []).append(table)
    previous = {text_key(u.get('sourceUnitName') or u['title']): u for u in old.get('units', [])}
    check = {'checkedAt': ref['receivedAt'], 'status': 'checked', 'changedUnitIds': [],
             'preservedUnitIds': [], 'unmatchedUnitIds': [], 'issues': []}
    for unit in notice['units']:
        key = text_key(unit.get('sourceUnitName') or unit['title']); prior = previous.get(key)
        rows = tables.get(key, [])
        if len(rows) != 1:
            check['unmatchedUnitIds'].append(unit['id'])
            if prior and prior.get('competition'):
                unit['competition'] = copy.deepcopy(prior['competition'])
                if 'competitionRecord' in prior: unit['competitionRecord'] = copy.deepcopy(prior['competitionRecord'])
                check['preservedUnitIds'].append(unit['id'])
            continue
        comp, record = competition_record(rows[0], detail, ref)
        prior_record = (prior or {}).get('competitionRecord', {})
        prior_comp = (prior or {}).get('competition')
        prior_stages = (prior_comp or {}).get('stages', prior_record.get('stages', []))
        new_stages = {s['name']: s for s in record.get('stages', [])}
        has_good = bool(prior_comp) or any(s.get('applicants') is not None or s.get('selected') is not None
                                         for s in prior_stages)
        stage_regression = any(any(stage.get(key) is not None and new_stages.get(stage['name'], {}).get(key) is None
                                   for key in ('applicants', 'selected', 'date')) for stage in prior_stages)
        regression = has_good and (record['status'] in ('extraction_failed', 'unverified') or
                                   (prior_comp is not None and comp is None) or stage_regression)
        if record['status'] == 'extraction_failed' or regression:
            check['issues'].append({'unitId': unit['id'], 'status': 'unverified' if regression and record['status'] != 'extraction_failed' else record['status'],
                                    'note': '이전에 확인한 공식 경쟁률·전형별 값이 새 표에서 빠지거나 해석되지 않아 이전 정상 결과를 보존했습니다.' if regression else record.get('note'),
                                    'previousGoodResultPreserved': has_good})
            if prior and has_good:
                unit['competition'] = copy.deepcopy(prior_comp)
                if 'competitionRecord' in prior: unit['competitionRecord'] = copy.deepcopy(prior_record)
                check['preservedUnitIds'].append(unit['id'])
            elif prior is None:
                unit['competition'] = comp; unit['competitionRecord'] = record
            continue
        incoming = {'competition': comp, 'competitionRecord': record}
        # Preserve historical evidence and reviewed definitions for an identical
        # source table; a new fetch is not a new semantic/manual review.
        same = prior is not None and result_payload(prior) == result_payload(incoming)
        if same:
            unit['competition'] = copy.deepcopy(prior.get('competition'))
            if 'competitionRecord' in prior: unit['competitionRecord'] = copy.deepcopy(prior['competitionRecord'])
        else:
            if prior and (prior.get('competition') or prior.get('competitionRecord')):
                history = copy.deepcopy(prior.get('competitionHistory', []))
                history.append({'supersededAt': ref['receivedAt'], 'competition': copy.deepcopy(prior.get('competition')),
                                'competitionRecord': copy.deepcopy(prior.get('competitionRecord'))})
                unit['competitionHistory'] = history[-12:]
            unit['competition'] = comp; unit['competitionRecord'] = record
            check['changedUnitIds'].append(unit['id'])
    if check['issues'] or check['unmatchedUnitIds']: check['status'] = 'partial'
    return check


def invalidate(unit, reason):
    unit = copy.deepcopy(unit)
    for name in FIELDS:
        previous = unit[name]
        unit[name] = {'value': None, 'status': 'unverified', 'sources': previous.get('sources', []),
                      'note': reason, 'previousVerified': previous, 'method': 'automatic_invalidation'}
    for key in ('competition', 'competitionRecord', 'evaluationRequirements', 'pastComparison'):
        if key in unit: unit['previous_' + key] = unit.pop(key)
    unit['competition'] = None
    unit['competitionRecord'] = {'status': 'unverified', 'note': reason}
    unit['pastComparison'] = {'status': 'unavailable', 'label': '비교자료 없음', 'sampleCount': 0}
    return unit


def merge_notice(root, old, result, detail, ref):
    incoming = result['notice']; sha = result['semanticHash']
    condition_sha = conditions_hash(detail); result_check = None
    results_only = False; baseline_differences = []
    if old is None:
        if incoming is None: return None, 'held'
        n = copy.deepcopy(incoming); kind = 'added'; changed = True; previous = None
    else:
        previous = baseline_hash(root, old)
        changed = previous is not None and previous != sha
        condition_previous = baseline_conditions_hash(root, old)
        results_only = changed and condition_previous is not None and condition_previous == condition_sha
        # A prior curated title/summary is not the old official response. Without
        # its hash, even an apparent metadata difference cannot prove a correction.
        dates = detail['announcement_period_raw'].split('~')
        comparisons = {'title': (old['title'], detail['title']), 'organization': (old['organization'], detail['org']),
                       'totalVacancies': (old['totalVacancies'], integer(detail['announcement_fields_raw']['채용인원'])),
                       'announcementEnd': (old.get('announcementPeriod', {}).get('end', old['deadline']), date_value(dates[1]))}
        if previous is None:
            baseline_differences = [key for key, (before, after) in comparisons.items() if before != after]
        kind = ('changed' if changed else 'baseline_difference' if baseline_differences else
                'baseline_established' if previous is None else 'unchanged')
        if results_only:
            n = copy.deepcopy(old)
            result_check = merge_results(n, old, detail, ref)
            kind = 'results_changed' if result_check['changedUnitIds'] else 'results_preserved' if result_check['issues'] else 'unchanged'
        elif old.get('collectionMethod') == 'automatic_public_html' and incoming is not None and previous is not None:
            n = copy.deepcopy(incoming if changed else old)
            # Stable content dates, even when the same notice is fetched again.
            if not changed: n['checkedAt'] = old['checkedAt']
            removed = [u for u in old['units'] if u['id'] not in {x['id'] for x in n['units']}]
            retired = {u['id']: u for u in old.get('retiredUnits', [])}
            retired.update({u['id']: u for u in removed})
            if retired: n['retiredUnits'] = list(retired.values())
        else:
            n = copy.deepcopy(old)
            if changed:
                reason = '공식 본문 또는 첨부 링크가 변경되어 기존 검토값의 현재 적용 여부가 미확인입니다. 이전 값은 기록에 보존했습니다.'
                n['units'] = [invalidate(u, reason) for u in n['units']]
                # Publish every newly identifiable unit, preserving legacy IDs only
                # where the exact raw unit name matches. Unmapped old units remain
                # in retiredUnits/history, never duplicated as current headcounts.
                if incoming:
                    legacy = {u.get('sourceUnitName'): u for u in old['units'] if u.get('sourceUnitName')}
                    n['units'] = copy.deepcopy(incoming['units']); retained = set()
                    for unit in n['units']:
                        match = legacy.get(unit['sourceUnitName'])
                        if match:
                            unit['id'] = match['id']; unit['previousVerifiedUnit'] = copy.deepcopy(match)
                            retained.add(match['id'])
                    retired = {u['id']: u for u in old.get('retiredUnits', [])}
                    retired.update({u['id']: copy.deepcopy(u) for u in old['units'] if u['id'] not in retained})
                    if retired: n['retiredUnits'] = list(retired.values())
                n['previousAttachments'] = copy.deepcopy(old['attachments'])
                n['attachments'] = copy.deepcopy((incoming or {}).get('attachments', [
                    {'name': '공식 첨부 ' + str(i + 1), 'url': url, 'status': 'unverified',
                     'note': '변경된 본문에서 링크만 확인. 첨부 내용 미확인.'}
                    for i, url in enumerate(detail.get('attachment_urls', []))]))
                for key in ('applicationPeriod', 'applicationStart', 'deadlineTime'):
                    n[key] = copy.deepcopy((incoming or {}).get(key))
                n['sourceNote'] = reason + ' 공고별 기존 확인시각은 유지하며 본문 자동 수신시각과 구분합니다.'
            elif previous is None:
                for unit in n['units']:
                    for field in FIELDS:
                        value = unit[field]
                        value.setdefault('previousReviewStatus', value['status'])
                        if value.get('value') is not None and value['status'] == 'confirmed': value['status'] = 'partial'
                        value['note'] = '이전 검토값입니다. 비교할 이전 본문이 없어 현재 적용 여부는 미확인입니다. ' + value.get('note', '')
                n['sourceNote'] = '본문 첫 자동 수신: 이전 원문이 없어 변경 여부를 비교하지 못했습니다. 기존 조건은 이전 검토값으로 표시합니다.'
                if baseline_differences:
                    n['sourceNote'] += ' 기존 요약과 새 공식 메타데이터의 차이는 발견했지만 조건 정정으로 단정하지 않았습니다.'
            n.update(organization=detail['org'], title=detail['title'], posted=date_value(detail['announcement_fields_raw']['등록일']),
                     totalVacancies=integer(detail['announcement_fields_raw']['채용인원']))
            n['announcementPeriod'] = {'start': date_value(dates[0]), 'end': date_value(dates[1]), 'scope': 'announcement'}
            if changed: n['deadline'] = (incoming or {}).get('deadline', date_value(dates[1]))
            auto_source = {'id': 'auto_alio', 'label': 'JOB-ALIO 상세 · 자동 수신', 'url': ref['url'],
                           'rawPath': ref['path'], 'sha256': ref['sha256'], 'receivedAt': ref['receivedAt']}
            n['sources'] = [s for s in n['sources'] if s['id'] != 'auto_alio'] + [auto_source]
            if changed and incoming:
                n['sources'] += [s for s in incoming['sources'] if s['id'] not in {x['id'] for x in n['sources']}]
    if not results_only:
        result_check = merge_results(n, old or {'units': []}, detail, ref)
        if result_check['changedUnitIds'] and kind in ('unchanged', 'baseline_established'):
            kind = 'results_changed'
    if old is not None and (results_only or old.get('collectionMethod') == 'automatic_public_html'):
        auto_source = {'id': 'auto_alio', 'label': 'JOB-ALIO 상세 · 자동 수신', 'url': ref['url'],
                       'rawPath': ref['path'], 'sha256': ref['sha256'], 'receivedAt': ref['receivedAt']}
        n['sources'] = [s for s in n['sources'] if s['id'] != 'auto_alio'] + [auto_source]
    n['autoSync'] = {'semanticHash': sha, 'conditionsHash': condition_sha,
                     'rawPath': ref['path'], 'rawSha256': ref['sha256'],
                     'lastFetchedAt': ref['receivedAt'], 'contentChangedAt': ref['receivedAt'] if changed else old.get('autoSync', {}).get('contentChangedAt'),
                     'baselineStatus': 'compared' if previous else 'first_capture_metadata_difference' if baseline_differences else 'first_capture',
                     'needsReview': bool(result['candidates']) or (old is not None and ((changed and not results_only) or previous is None)),
                     'note': '본문·결과 표만 자동 수신. 첨부 파일 내용은 재확인하지 않았습니다. 기존 AI 검토시각은 갱신하지 않습니다.'}
    if result_check is not None: n['autoSync']['resultCheck'] = result_check
    if baseline_differences:
        n['autoSync']['baselineDifference'] = {'fields': baseline_differences, 'status': 'unverified',
            'note': '이전 원문이 없어 요약과 현재 공식 메타데이터의 차이를 실제 조건 변경으로 판단하지 않았습니다. 기존 모집단위 확인값을 보존했습니다.'}
    elif old and old.get('autoSync', {}).get('baselineDifference'):
        n['autoSync']['baselineDifference'] = copy.deepcopy(old['autoSync']['baselineDifference'])
    validate_data({'schemaVersion': '0.1', 'asOfDate': '2026-09-13', 'notices': [n]})
    return n, kind


def lifecycle(notice, timestamp):
    now = dt.datetime.fromisoformat(timestamp).astimezone(KST)
    clock = notice.get('deadlineTime')
    if clock and re.fullmatch(r'\d{2}:\d{2}', clock):
        end = dt.datetime.fromisoformat(notice['deadline'] + 'T' + clock + ':00').replace(tzinfo=KST)
        basis = '저장된 마감일·시각(KST) 경과; 원문 정정 여부와 별개'
    else:
        end = dt.datetime.fromisoformat(notice['deadline'] + 'T23:59:59.999999').replace(tzinfo=KST)
        basis = '마감 시각 미확인: 저장된 마감일 다음 날부터 지난 공고로 분류'
    return {'status': 'deadline_elapsed' if now >= end else 'open', 'evaluatedDate': now.date().isoformat(), 'basis': basis}


def held_summaries(state, limit=20):
    """Show official links for actually-read ambiguous notices, without fake units."""
    reasons = {
        'duplicate_unit_name': '동일 모집단위명이 중복되어 대응 미확인',
        'office_role_not_identifiable_from_unit_name': '모집단위명만으로 사무직 여부 미확인',
        'office_and_other_role_mixed_in_unit_name': '사무직과 다른 직무가 함께 표기되어 분리 미확인',
        'mixed_or_unverified_unit_employment': '해당 모집단위의 기간제·계약직 여부 미확인',
        'no_recruitment_unit_table': '개별 모집단위 표 해석 미완료',
    }
    groups = {}
    for candidate in state.get('candidates', {}).values():
        nid = candidate['noticeId']; entry = state.get('inbox', {}).get(nid, {})
        reason = candidate.get('reason')
        if entry.get('state') != 'held' or not entry.get('lastSuccessAt') or reason not in reasons: continue
        # Definite non-office names do not become a public office waiting list.
        if reason == 'office_role_not_identifiable_from_unit_name' and re.search(
                r'연구|검사|간호|의사|정비|시설|조리|운전|건축|전기|토목|기술|선박', candidate.get('rawUnitName') or ''):
            continue
        row = entry['row']
        item = groups.setdefault(nid, {'id': nid, 'organization': row['org'], 'title': row['title'],
            'sourceUrl': row['source_url'], 'lastFetchedAt': entry['lastSuccessAt'], 'reasons': [], 'status': 'unverified'})
        if reasons[reason] not in item['reasons']: item['reasons'].append(reasons[reason])
    items = sorted(groups.values(), key=lambda item: item['lastFetchedAt'], reverse=True)
    return {'items': items[:limit], 'total': len(items), 'shownLimit': limit}


def apply_batch(root, data, state, batch, batch_path, mode, at=None):
    timestamp = at or batch['startedAt']; config = batch['config']
    state = copy.deepcopy(state); state.setdefault('inbox', {}); state.setdefault('candidates', {})
    by_id = {n['id']: copy.deepcopy(n) for n in data['notices']}
    report = {'startedAt': timestamp, 'mode': mode, 'batch': str(batch_path), 'records': [], 'errors': [],
              'newRemoteRequests': batch.get('actualRequests', 0) if mode == 'live' else 0}
    success = 0; lists = 0; added = 0; new_slots = 0; failed = set(); changed = []
    initial_ids = set(batch.get('initialNoticeIds', [n['id'] for n in data['notices']]))
    for item in batch['items']:
        ref = item['ref']; event = item['event']
        if event.get('stop_origin'): state['originBlocked'] = {'receivedAt': ref['receivedAt'], 'url': ref['url'], 'httpStatus': event.get('http_status')}
        try:
            raw = source_data(root, ref); accepted(raw, event)
            if item['kind'] == 'list':
                parsed = transport.parse_list(raw, ref['url'], item['page']); lists += 1
                for row in parsed['rows']:
                    entry = state['inbox'].setdefault(row['id'], {'state': 'queued'})
                    prior = entry.get('row', {})
                    if row_signature(prior) != row_signature(row):
                        entry.pop('lastAttemptAt', None)
                        if row['id'] not in by_id: entry['state'] = 'queued'
                    entry.update(row=row, lastSeenAt=ref['receivedAt'])
                continue
            expected = item['expected']; nid = expected['id']
            entry = state['inbox'].setdefault(nid, {'row': expected})
            entry['lastAttemptAt'] = ref['receivedAt']
            detail = parse_current_detail(raw, ref['url'], expected)
            result = normalize(detail, ref)
            for key in [k for k, v in state['candidates'].items() if v['noticeId'] == nid]: del state['candidates'][key]
            for candidate in result['candidates']: state['candidates'][candidate['id']] = candidate
            if nid not in initial_ids and result['notice'] is not None:
                if new_slots >= config['newNoticeLimit']:
                    entry['state'] = 'queued'; report['records'].append({'id': nid, 'status': 'deferred_by_new_notice_limit'}); success += 1; continue
                new_slots += 1
            old = by_id.get(nid)
            notice, kind = merge_notice(root, old, result, detail, ref)
            entry.update(lastSuccessAt=ref['receivedAt'], state='held' if notice is None else 'published')
            if notice is not None:
                if kind in ('changed', 'results_changed'):
                    changed.append(nid)
                    save(root / 'evidence/automatic_refresh_20260913/history' / (uuid.uuid4().hex + '-' + nid + '.json'),
                         {'changedAt': ref['receivedAt'], 'before': old, 'afterRaw': ref})
                if old is None: added += 1
                by_id[nid] = notice
                result_check = notice.get('autoSync', {}).get('resultCheck', {})
                issues = result_check.get('issues', []) + [
                    {'unitId': unit_id, 'status': 'unverified', 'note': '현재 공식 표와 모집단위명이 정확히 대응하지 않아 결과를 연결하지 않았습니다.'}
                    for unit_id in result_check.get('unmatchedUnitIds', [])]
                if issues:
                    report['errors'].append({'id': nid, 'phase': 'result_fields', 'issues': issues,
                                             'error': '결과 표 일부 미확인·추출 실패. 기존 정상 결과를 보존했습니다.',
                                             'existingNoticePreserved': True})
            report['records'].append({'id': nid, 'status': kind, 'unitCount': len(notice['units']) if notice else 0})
            success += 1
        except Exception as exc:
            nid = item.get('expected', {}).get('id')
            if nid:
                failed.add(nid)
                state['inbox'].setdefault(nid, {'row': item['expected']}).update(lastAttemptAt=ref.get('receivedAt'), lastError=str(exc))
            report['errors'].append({'id': nid, 'phase': item['kind'], 'error': str(exc), 'existingNoticePreserved': nid in by_id})
    report['errors'] += [x for x in batch.get('errors', []) if x not in report['errors']]
    outcome = 'failed' if not lists and not success else 'partial_success' if report['errors'] else 'success'
    candidate = copy.deepcopy(data)
    deadline_events = []
    if lists or success:
        day = dt.datetime.fromisoformat(timestamp).astimezone(KST).date().isoformat()
        for n in by_id.values():
            if n['id'] in failed: continue
            life = lifecycle(n, timestamp)
            old_status = n.get('lifecycle', {}).get('status', 'deadline_elapsed' if n['deadline'] < data['asOfDate'] else 'open')
            if old_status != life['status']: deadline_events.append({'id': n['id'], 'from': old_status, 'to': life['status'], 'basis': life['basis']})
            n['lifecycle'] = life
        candidate['notices'] = list(by_id.values()); candidate['asOfDate'] = day
        state['nextPage'] = batch.get('nextPage', state.get('nextPage', 2))
    old_refresh = data.get('refresh', {})
    report.update(status=outcome, noticeCount=len(candidate['notices']), added=added, changedIds=changed,
                  deadlineEvents=deadline_events, actualListPages=lists,
                  preservedFailedIds=sorted(failed), finishedAt=batch.get('finishedAt', timestamp))
    queued = sum(v.get('state') == 'queued' for v in state['inbox'].values())
    held = held_summaries(state)
    candidate['refresh'] = {'status': outcome, 'mode': mode, 'lastAttemptAt': timestamp,
        'lastSuccessAt': timestamp if outcome == 'success' else old_refresh.get('lastSuccessAt'),
        'candidatesCount': len(state['candidates']) + queued, 'lastRunSummary': report,
        'coverage': {'description': 'JOB-ALIO 최근60일 이내 진행중·비정규직·경영회계사무 목록, 목록최대2페이지·상세최대4건/회. 저장된 마감공고도 결과를 순환 재확인합니다. 전체공고 수집 아님.',
                     'queuedNoticeCount': queued, 'heldUnitCount': len(state['candidates']), 'nextPage': state.get('nextPage', 2),
                     'closedRecheck': {'recentDays': 30, 'recentOrUnknownIntervalDays': 1, 'olderConfirmedIntervalDays': 30,
                                       'ageLimitDays': None, 'note': '각 실행의 상세4건 한도 안에서 신규·진행중·최근/결과미확인 마감·오래된 마감에 우선 1자리씩 배분. 대기량에 따라 지연됩니다.'},
                     'requestLimit': config['requestLimit'], 'automaticRetries': 0}}
    candidate['refresh']['coverage'].update(heldNotices=held['items'], heldNoticesTotal=held['total'],
                                            heldNoticesShownLimit=held['shownLimit'])
    state['schemaVersion'] = 'automatic-refresh-state-0.2'
    outputs = screen_outputs(candidate, root)
    outputs.update({root / 'data/notices.json': encoded(candidate), root / 'data/refresh_state.json': encoded(state),
                    root / 'data/refresh_status.json': encoded(report)})
    commit_files(outputs)
    return report


def run(root=ROOT, replay=None, at=None, recorder_class=transport.Recorder):
    root = Path(root)
    with single_writer(root):
        data = load(root / 'data/notices.json'); validate_data(data)
        state_path = root / 'data/refresh_state.json'
        state = load(state_path) if state_path.exists() else {'inbox': {}, 'candidates': {}, 'nextPage': 2}
        # Both active and closed notices persist beyond the bounded list pages.
        for n in data['notices']:
            state['inbox'].setdefault(n['id'], {'state': 'published', 'row': {'id': n['id'], 'idx': str(n['idx']),
                'org': n['organization'], 'title': n['title'], 'source_url': 'https://job.alio.go.kr/recruitview.do?idx=' + str(n['idx'])}})
        config = config_for(root); started = at or utc()
        if replay:
            batch_path = Path(replay)
            if not batch_path.is_absolute(): batch_path = root / batch_path
            batch = load(batch_path)
            if batch.get('schemaVersion') != 'automatic-capture-0.1': raise ValueError('지원하지 않는 원문 묶음')
            mode = 'replay'
        else:
            try:
                batch, batch_path = capture(root, config, data, state, started, recorder_class)
            except Exception as exc:
                batch = {'schemaVersion': 'automatic-capture-0.1', 'startedAt': started,
                         'config': config, 'items': [], 'actualRequests': 0 if state.get('originBlocked') else None,
                         'errors': [{'phase': 'preflight_or_capture', 'error': str(exc)}]}
                batch_path = root / 'evidence/automatic_refresh_20260913/errors' / (uuid.uuid4().hex + '.json')
                save(batch_path, batch)
            mode = 'live'
        report = apply_batch(root, data, state, batch, batch_path.relative_to(root), mode, at=at)
        out = root / 'evidence/automatic_refresh_20260913/runs' / (uuid.uuid4().hex + '.json')
        save(out, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', type=Path, help='Saved batch.json; no remote requests')
    args = parser.parse_args()
    try:
        result = run(replay=args.replay)
        return 0 if result['status'] == 'success' else 2 if result['status'] == 'partial_success' else 1
    except Exception as exc:
        report = {'status': 'failed', 'error': str(exc), 'attemptedAt': utc(), 'previousOutputsPreserved': True}
        save(ROOT / 'data/refresh_error.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
