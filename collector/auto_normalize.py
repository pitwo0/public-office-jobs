#!/usr/bin/env python3
"""Conservative, deterministic JOB-ALIO detail normalization, without AI/network.

Input is ``probe_routes.parse_detail`` output and a captured-source reference.
Only identifiable office recruitment units with temporary-employment evidence
are published. Ambiguous units remain candidates. Narrative conditions are
copied with announcement scope; they are never promoted to unit eligibility.
"""
import copy
import datetime
import hashlib
import json
import re
import unicodedata
from urllib.parse import urljoin, urlparse

VERSION = 'auto-normalize-0.1'
SOURCE_ID = 'auto_alio'
FIELDS = ('vacancies', 'duties', 'requirements', 'computer', 'english',
          'workplace', 'pay', 'contract', 'selection', 'documents')
OFFICE = re.compile(r'사무|행정|경영|예산|회계|인사|총무|홍보')
AMBIGUOUS_OFFICE = re.compile(r'연구|교수|강사|교원|의사|간호|정비|개발자|운전|조리|시설관리')
NON_TARGET = re.compile(r'무기\s*계약|(?<!비)정규직|인턴')
TEMPORARY = re.compile(r'육아\s*휴직\s*대체|휴직\s*대체|기간제|계약직|비정규직')
DATE = re.compile(r'(?<!\d)(\d{4}|\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})(?:\s*[.일])?(?!\d)')
SHORT_DATE = re.compile(r'^\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})(?:\s*[.일])?(?!\d)')


def text_key(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', str(value))).strip()


def unit_id(raw_name):
    """Stable across page ordering, whitespace and code execution dates."""
    return 'auto-' + hashlib.sha256(text_key(raw_name).encode('utf-8')).hexdigest()[:16]


def semantic_hash(detail):
    """Only recruitment content, excluding request metadata and site navigation."""
    keys = ('id', 'idx', 'org', 'title', 'announcement_period_raw',
            'announcement_fields_raw', 'application_period', 'sections',
            'unit_results_raw', 'institution_notice_urls', 'attachment_urls')
    def clean(value):
        if isinstance(value, str):
            return '\n'.join(text_key(line) for line in value.replace('\r\n', '\n').split('\n')).strip()
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value
    payload = clean({k: detail.get(k) for k in keys})
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def conditions_hash(detail):
    """Result rows may change without changing eligibility, pay or attachments."""
    content = copy.deepcopy(detail)
    content['unit_results_raw'] = [{'unit_name_raw': text_key(u['unit_name_raw'])}
                                   for u in detail.get('unit_results_raw', [])]
    return semantic_hash(content)


def date_value(raw):
    match = DATE.fullmatch(raw.strip())
    if not match:
        raise ValueError('날짜 형식 미인식: ' + raw)
    year, month, day = map(int, match.groups())
    return datetime.date(year + 2000 if year < 100 else year, month, day).isoformat()


def integer(raw):
    raw = str(raw).strip()
    if raw in ('', '-'):
        return None
    if not re.fullmatch(r'\d+\s*명?', raw):
        raise ValueError('인원 형식 미인식: ' + raw)
    return int(re.sub(r'\s*명$', '', raw))


def field(value=None, status='unverified', note='모집단위별 근거를 자동으로 해석하지 못했습니다.', **extra):
    return dict(value=value, status=status, note=note, sources=[SOURCE_ID],
                method='deterministic_public_html_parser', **extra)


def application_period(detail):
    """Parse explicit 접수기간 lines only. No inferred registration/application date."""
    lines = []
    for section, body in detail.get('sections', {}).items():
        for line in body.splitlines():
            if re.search(r'접수\s*기간\s*[:：]', line):
                lines.append((section, line.strip()))
    result = dict(start=None, end=None, endTime=None, status='unverified',
                  scope='announcement', sources=[SOURCE_ID],
                  raw='\n'.join(line for _, line in lines) or None,
                  note='명시된 접수기간을 단일 기간으로 해석하지 못했습니다. 공고기간으로 대체하지 않습니다.')
    if len(lines) != 1:
        return result
    section, line = lines[0]
    body = re.split(r'접수\s*기간\s*[:：]', line, maxsplit=1)[1]
    pieces = re.split(r'~|∼|～', body)
    if len(pieces) != 2:
        return result
    left = DATE.search(pieces[0]); right = DATE.search(pieces[1])
    if not left:
        return result
    # A prefixed unit name is not silently treated as an all-unit period.
    prefix = re.split(r'접수\s*기간\s*[:：]', line, maxsplit=1)[0]
    if re.search(r'[가-힣]', prefix):
        return result
    try:
        year, month, day = map(int, left.groups())
        year = year + 2000 if year < 100 else year
        start = datetime.date(year, month, day)
        if right:
            y, m, d = map(int, right.groups()); y = y + 2000 if y < 100 else y
        else:
            short = SHORT_DATE.match(pieces[1])
            if not short:
                return result
            y = year; m, d = map(int, short.groups())
        end = datetime.date(y, m, d)
        if end < start:
            return result
        clocks = re.findall(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)', pieces[1])
        if len(clocks) > 1:
            return result
        clock = None
        if clocks:
            h, minute = map(int, clocks[0]); datetime.time(h, minute)
            clock = f'{h:02d}:{minute:02d}'
        result.update(start=start.isoformat(), end=end.isoformat(), endTime=clock,
                      status='partial', evidenceSection=section,
                      note='공고 본문의 단일 접수기간 원문을 자동 정규화했습니다. 모집단위별 예외·첨부 정정 여부는 미확인입니다.')
    except ValueError:
        pass
    return result


def employment_evidence(name, detail):
    """Mixed employment needs evidence local to the exact unit, never NCS/title alone."""
    if NON_TARGET.search(name):
        return None, 'not_target_employment', name
    if TEMPORARY.search(name):
        return TEMPORARY.search(name).group(), 'unit_name', name
    names = [text_key(u['unit_name_raw']) for u in detail['unit_results_raw']]
    key = text_key(name)
    for section, body in detail['sections'].items():
        for line in body.splitlines():
            clean = text_key(line).lstrip('-•·ㅇ ')
            # Only a line beginning with this exact unit label followed by colon.
            if not re.match(re.escape(key) + r'\s*[:：]', clean):
                continue
            if any(other != key and other in clean for other in names):
                continue
            value = re.split(r'[:：]', clean, maxsplit=1)[1].strip()
            explicit_type = re.match(r'^(?:고용형태|채용형태|고용구분)\s*[:：]?\s*(.*)', value)
            if explicit_type:
                value = explicit_type[1]
            elif re.search(r'경력|경험|재직|소지|지원\s*자격', value):
                # A requirement for previous contract work is not this job's type.
                continue
            elif not re.match(r'^(?:육아\s*휴직\s*대체|휴직\s*대체|기간제|계약직|비정규직)', value):
                continue
            if NON_TARGET.search(value):
                return None, 'not_target_employment', line
            match = TEMPORARY.search(value)
            if match and (explicit_type or re.search(r'채용|근로자|직원|고용', value)):
                return match.group(), 'unit_explicit_line:' + section, line
    raw = detail['announcement_fields_raw'].get('고용형태', '')
    tokens = [text_key(x) for x in re.split(r'[,;/]', raw) if x.strip()]
    # Exact accepted employment types, not substring of permanent mixed lists.
    if len(tokens) == 1 and tokens[0] in ('비정규직', '계약직', '기간제', '기간제근로자'):
        return tokens[0], 'announcement_single_employment', raw
    return None, 'mixed_or_unverified_unit_employment', raw


def competition_record(table, detail, ref):
    rows = copy.deepcopy(table.get('table_rows_raw', []))
    evidence = dict(sourceUrl=ref['url'], rawPath=ref['path'], sha256=ref['sha256'],
                    receivedAt=ref['receivedAt'], rawUnitName=table['unit_name_raw'], rows=rows,
                    method='deterministic_public_html_parser')
    result = dict(status='unverified', officialValueRaw=None, officialLabel='최종 경쟁률',
                  scope='recruitment_unit', definitionStatus='unverified',
                  numeratorDefinition=None, denominatorDefinition=None, stages=[], evidence=evidence)
    try:
        required = {'구분', '응시인원', '선발인원', '결과 확정일'}
        headers = [r for r in rows if required <= set(r)]
        if len(headers) != 1:
            raise ValueError('전형 표 열 제목 단일 대응 실패')
        header = headers[0]; ix = {k: header.index(k) for k in required}
        stages = []
        for row in rows[rows.index(header) + 1:]:
            if row and row[0] == '경쟁률':
                continue
            if len(row) != len(header):
                raise ValueError('전형 표 열 수 불일치')
            raw_date = row[ix['결과 확정일']]
            stages.append(dict(name=row[ix['구분']], applicants=integer(row[ix['응시인원']]),
                               selected=integer(row[ix['선발인원']]),
                               date=None if raw_date in ('', '-') else date_value(raw_date)))
        if not stages:
            raise ValueError('전형 표 행 없음')
        if len({s['name'] for s in stages}) != len(stages):
            raise ValueError('중복 전형 이름으로 행 대응 실패')
        result['stages'] = stages
        official = table.get('official_ratio', {})
        ratio = official.get('value_raw')
        result['officialLabel'] = official.get('label', '최종 경쟁률')
        if official.get('status') == '추출 실패':
            raise ValueError('경쟁률 행 형식 미인식')
        if ratio is None:
            if official.get('status') != '미공개':
                raise ValueError('경쟁률 공란 여부 미확인')
            result.update(status='not_disclosed', note='확인한 해당 모집단위 결과 표의 공식 경쟁률 공란입니다. 0:1이 아닙니다.')
            return None, result
        if not re.fullmatch(r'\d+(?:\.\d+)?', str(ratio)):
            raise ValueError('경쟁률 숫자 형식 미인식')
        number = float(ratio); number = int(number) if number.is_integer() else number
        result.update(status='confirmed', officialValueRaw=str(ratio))
        competition = dict(label='JOB-ALIO ' + result['officialLabel'], published=number,
                           publishedRaw=str(ratio), scope='recruitment_unit',
                           definition='공식 표기값입니다. 산식·분자/분모 정의는 미확인입니다. 전형별 인원과 구분합니다.',
                           definitionStatus='unverified', sources=[SOURCE_ID], stages=stages, evidence=evidence)
        if stages[-1]['selected'] == 0:
            competition['note'] = '최종 선발 0명입니다. 공식 표기값을 지원자 없음이나 낮은 경쟁률로 해석하지 않습니다.'
        return competition, result
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result.update(status='extraction_failed', note=str(exc))
        return None, result


def normalize(detail, ref):
    """Return {notice, candidates, semanticHash}; no source requests or AI rules."""
    sha = semantic_hash(detail)
    meta = detail['announcement_fields_raw']
    periods = detail['announcement_period_raw'].split('~')
    if len(periods) != 2:
        raise ValueError('공고기간 형식 미인식')
    announcement_start, deadline = map(date_value, periods)
    posted = date_value(meta['등록일'])
    if posted > deadline or announcement_start > deadline:
        raise ValueError('공고 날짜 순서 오류')
    if detail['id'] != 'job-alio-' + str(detail['idx']):
        raise ValueError('공고 ID 불일치')
    source = dict(id=SOURCE_ID, label='JOB-ALIO 상세 · 코드 수신·자동 정리', url=ref['url'],
                  rawPath=ref['path'], sha256=ref['sha256'], receivedAt=ref['receivedAt'])
    candidates = []; units = []
    names = [text_key(u['unit_name_raw']) for u in detail['unit_results_raw']]
    regions = list(dict.fromkeys(text_key(x) for x in re.split(r'[,;/]', meta['근무지']) if x.strip()))
    for table in detail['unit_results_raw']:
        name = table['unit_name_raw']; key = text_key(name)
        employment, reason, quote = employment_evidence(name, detail)
        rejected = None
        if names.count(key) != 1:
            rejected = 'duplicate_unit_name'
        elif not OFFICE.search(name):
            rejected = 'office_role_not_identifiable_from_unit_name'
        elif AMBIGUOUS_OFFICE.search(name):
            rejected = 'office_and_other_role_mixed_in_unit_name'
        elif not employment:
            rejected = reason
        if rejected:
            candidates.append(dict(id=detail['id'] + ':' + unit_id(name), noticeId=detail['id'],
                                   rawUnitName=name, status='held', reason=rejected,
                                   evidence=copy.deepcopy(ref), employmentEvidence=quote))
            continue
        u = dict(id=unit_id(name), title=name, sourceUnitName=name, employment=employment,
                 regions=regions, **{key: field() for key in FIELDS})
        u['vacancies'] = field(note='해당 모집단위의 계획 모집인원 미확인. 공고 전체 인원·전형별 선발인원은 대입하지 않습니다.')
        # Exact count in a unit label can be recorded; never use table selection counts.
        counts = re.findall(r'(?:채용|모집)\s*인원\s*[:：]?\s*(\d+)\s*명', name)
        if len(counts) == 1:
            u['vacancies'] = field(int(counts[0]), 'confirmed', '공식 모집단위명에 명시된 모집인원.',
                                   scope='recruitment_unit', evidenceLocator='모집단위명')
        for field_name, section in (('requirements', '응시자격'), ('selection', '전형절차/방법')):
            value = detail['sections'].get(section)
            if value:
                u[field_name] = field(value, 'partial',
                    '공고 전체 본문 원문입니다. 다른 모집단위 조건이 섞일 수 있으며, 선택 단위에 모두 적용된다고 확정하지 않습니다.',
                    scope='announcement', evidenceSection=section, evidenceLocator='공고 전체 · ' + section)
        u['workplace'] = field(meta['근무지'], 'partial', '공고 전체 근무지역입니다. 해당 단위의 배치 지역·상세 주소는 미확인입니다.',
                               scope='announcement', evidenceLocator='공고 메타데이터 · 근무지')
        u['competition'], u['competitionRecord'] = competition_record(table, detail, ref)
        u['employmentEvidence'] = dict(scope='announcement' if reason.startswith('announcement') else 'recruitment_unit',
                                        method=reason, value=employment, quote=quote, sources=[SOURCE_ID])
        u['evaluationRequirements'] = dict(
            eligibility={k: dict(value=None, status='unverified') for k in ('computerRequired', 'englishRequired', 'additionalRequirements')},
            scoredCriteria=[], englishEvaluation=dict(status='unverified', points=None, tests=None),
            scoreFormulaImplemented=False, reviewMethod='not_semantically_reviewed',
            note='지원 필수조건과 배점·가점·적용전형·상한·중복조건은 자동 판정하지 않았습니다.')
        u['comparisonContext'] = dict(institution=detail['org'], job=name, rawUnitName=name,
            regions=regions, regionsScope='announcement', employment=employment,
            recruitmentCategory=dict(value=meta.get('채용구분'), status='partial', scope='announcement'),
            ratioScope='recruitment_unit', definitionStatus='unverified')
        u['pastComparison'] = dict(status='unavailable', label='비교자료 없음', sampleCount=0,
                                  comparisonPeriod=dict(start=None, end=None), matches=[], conditionMatch=None,
                                  note='검증해 연결한 과거 비교자료가 없으며 0:1 또는 예상 경쟁률이 아닙니다.')
        u['provenance'] = dict(method=VERSION, semanticReview='none',
                               automaticProcessing='공식 모집단위명·고용형태 규칙 확인, 본문 원문 복사, 단위별 결과 표 파싱',
                               rawDetail=copy.deepcopy(ref))
        units.append(u)
    if not detail['unit_results_raw']:
        candidates.append(dict(id=detail['id'] + ':no-units', noticeId=detail['id'], rawUnitName=None,
                               status='held', reason='no_recruitment_unit_table', evidence=copy.deepcopy(ref)))
    if not units:
        return dict(notice=None, candidates=candidates, semanticHash=sha)
    sources = [source]
    for number, url in enumerate(dict.fromkeys(detail.get('institution_notice_urls', [])), 1):
        if urlparse(url).scheme in ('https', 'http'):
            sources.append(dict(id='auto_original_' + str(number), label='기관 원문 링크 · 내용 미확인', url=url))
    attachments = []
    for url in dict.fromkeys(detail.get('attachment_urls', [])):
        url = urljoin(ref['url'], url)
        if urlparse(url).scheme in ('https', 'http'):
            attachments.append(dict(name='공식 첨부 ' + str(len(attachments) + 1), url=url,
                                    status='unverified', note='상세 페이지의 링크만 자동 확인. 파일 수신·내용 해석 미수행.'))
    app = application_period(detail)
    n = dict(id=detail['id'], idx=int(detail['idx']), organization=detail['org'], title=detail['title'],
             posted=posted, deadline=deadline, deadlineTime=None,
             announcementPeriod=dict(start=announcement_start, end=deadline, scope='announcement'),
             applicationPeriod=app, applicationStart=app['start'], regions=regions,
             totalVacancies=integer(meta['채용인원']),
             employment=' · '.join(dict.fromkeys(u['employment'] for u in units)),
             units=units, sources=sources, attachments=attachments, checkedAt=ref['receivedAt'],
             collectionMethod='automatic_public_html',
             collectionLabel='코드 원격 수신 → 규칙 기반 모집단위 정리 → 저장·화면 생성 · GPT 미사용',
             sourceNote='자동으로 확인 가능한 본문·결과 표만 정리했습니다. 첨부·모집단위별 상세 조건은 미확인이며, 공고 전체 원문은 별도로 표시합니다.',
             announcementMetadata=dict(raw=copy.deepcopy(meta), rawApplicationPeriod=copy.deepcopy(detail.get('application_period')),
                 sourcePath=ref['path'], sourceSha256=ref['sha256'], sourceReceivedAt=ref['receivedAt'], method=VERSION),
             autoNormalization=dict(version=VERSION, semanticHash=sha, generatedFrom=copy.deepcopy(ref),
                                    aiUsed=False, heldUnitCount=len(candidates)))
    if app['end']:
        n['deadline'] = app['end']; n['deadlineTime'] = app['endTime']
    return dict(notice=n, candidates=candidates, semanticHash=sha)
