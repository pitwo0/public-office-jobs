#!/usr/bin/env python3
"""Build a dependency-free HTML with reviewed data embedded; no network."""
from pathlib import Path
import hashlib,json,os,tempfile
ROOT=Path(__file__).resolve().parent

def validate_data(data):
    import datetime,re
    if data.get('schemaVersion')!='0.1':raise ValueError('Unsupported schema')
    datetime.date.fromisoformat(data['asOfDate'])
    ids=[n['id'] for n in data['notices']]
    if len(set(ids))!=len(ids):raise ValueError('중복 공고 ID')
    def count(x):return x is None or (type(x) is int and x>=0)
    for n in data['notices']:
        if not re.fullmatch(r'job-alio-[0-9]+',n['id']):raise ValueError('Invalid notice ID')
        if not count(n['totalVacancies']):raise ValueError('Invalid total headcount')
        for key in ['posted','deadline']:datetime.date.fromisoformat(n[key])
        if n['posted']>n['deadline']:raise ValueError('Invalid date range')
        if n.get('applicationStart'):datetime.date.fromisoformat(n['applicationStart'])
        if not n['units']:raise ValueError('Missing unit')
        unit_ids=[u['id'] for u in n['units']]
        if len(set(unit_ids))!=len(unit_ids):raise ValueError('중복 모집단위 ID')
        source_ids={s['id'] for s in n['sources']}
        for s in n['sources']+n['attachments']:
            if not s['url'].startswith(('https://','http://')):raise ValueError('Unsafe source URL')
        for u in n['units']:
            if not count(u['vacancies']['value']):raise ValueError('Invalid unit headcount')
            for key in ['vacancies','duties','requirements','computer','english','workplace','pay','contract','selection','documents']:
                if u[key]['status'] not in ['confirmed','partial','not_disclosed','not_stated','unverified','extraction_failed']:raise ValueError('Invalid field status')
                if not set(u[key].get('sources',[]))<=source_ids:raise ValueError('Missing field evidence source')
                for part in u[key].get('components',[]):
                    if part['status'] not in ['confirmed','partial','not_disclosed','not_stated','unverified','extraction_failed']:raise ValueError('Invalid component status')
                    if part['status']=='confirmed' and part.get('value') is None:raise ValueError('Confirmed component lacks a value')
            if u.get('competition'):
                c=u['competition']
                if type(c['published']) not in (int,float) or c['published']<0:raise ValueError('Invalid competition ratio')
                for st in c['stages']:
                    if not count(st['applicants']) or not count(st['selected']):raise ValueError('Invalid stage count')

def render(data,root=ROOT):
    """Validate and render in memory before changing the saved data or screen."""
    root=Path(root)
    validate_data(data)
    payload=json.dumps(data,ensure_ascii=False).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    return (root/'ui/template.html').read_text(encoding='utf-8').replace('/*__CSS__*/',(root/'ui/styles.css').read_text(encoding='utf-8')).replace('/*__DATA__*/',payload).replace('/*__JS__*/',(root/'ui/app.js').read_text(encoding='utf-8'))

def commit_files(outputs):
    """Stage all files, then replace. Roll back caught write errors.

    One invocation at a time; this is not a multi-process database or a guarantee
    against power loss while several file replacements are in progress.
    """
    staged={};backups={};replaced=[]
    try:
        for path,raw in outputs.items():
            path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
            backups[path]=path.read_bytes() if path.exists() else None
            fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.pending',dir=path.parent)
            staged[path]=Path(name)
            with os.fdopen(fd,'wb') as f:f.write(raw)
        for path,pending in staged.items():
            os.replace(pending,path);replaced.append(path)
    except Exception:
        for path in reversed(replaced):
            if backups[path] is None:path.unlink(missing_ok=True)
            else:path.write_bytes(backups[path])
        raise
    finally:
        for path in staged.values():path.unlink(missing_ok=True)

def screen_outputs(data,root=ROOT):
    """Local review copy plus a deliberately limited Pages publication tree."""
    from deployment.public_data import public_data
    root=Path(root)
    published=public_data(data)
    # A no-op rebuild keeps the exact published artifact and its true creation
    # time. This also makes a replay idempotent while source-check times remain
    # part of the data and therefore do advance the fingerprint when changed.
    fingerprint=hashlib.sha256()
    fingerprint.update(json.dumps({k:v for k,v in published.items() if k!='presentation'},
                                  sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
    for name in ('template.html','styles.css','app.js'):
        fingerprint.update(name.encode('utf-8'))
        fingerprint.update((root/'ui'/name).read_text(encoding='utf-8').encode('utf-8'))
    input_hash=fingerprint.hexdigest()
    try:
        previous=json.loads((root/'dist/data/notices.json').read_text(encoding='utf-8'))
        if previous.get('presentation',{}).get('inputHash')==input_hash:
            published['presentation']['generatedAt']=previous['presentation']['generatedAt']
    except (FileNotFoundError,ValueError,KeyError,TypeError):
        pass
    published['presentation']['inputHash']=input_hash
    local_html=render(data,root).encode('utf-8')
    public_html=render(published,root).encode('utf-8')
    public_json=(json.dumps(published,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    return {root/'index.html':local_html,root/'dist/index.html':public_html,
            root/'dist/data/notices.json':public_json,root/'dist/.nojekyll':b''}

outputs_for=screen_outputs

def publish(data,root=ROOT):
    root=Path(root)
    payload=(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    outputs=screen_outputs(data,root);outputs[root/'data/notices.json']=payload
    commit_files(outputs)
    return len(outputs[root/'dist/index.html'])

def build(root=ROOT):
    root=Path(root);data=json.loads((root/'data/notices.json').read_text(encoding='utf-8'))
    outputs=screen_outputs(data,root)
    commit_files(outputs)
    print(f"Built dist/index.html + data/notices.json: {len(data['notices'])} real notices; {len(outputs[root/'dist/index.html'])} bytes")
if __name__=='__main__':build()
