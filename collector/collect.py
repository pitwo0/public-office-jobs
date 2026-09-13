#!/usr/bin/env python3
"""JOB-ALIO public HTML collector. stdlib only; failed fetch never means no jobs.

Live output is an inbox, not reviewed eligibility data. No attachment interpretation.
"""
from pathlib import Path
from html.parser import HTMLParser
from urllib.request import urlopen,Request
from urllib.parse import urlparse,parse_qs,urljoin
import argparse,datetime,hashlib,json,re,sys,time
ROOT=Path(__file__).resolve().parents[1]
LIST_URL='https://job.alio.go.kr/recruit.do?replacement=Y'
ALLOWED_HOST='job.alio.go.kr'

def now():return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
def atomic_json(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');tmp.replace(path)
def upsert(existing,incoming,key='id'):
    result={x[key]:x for x in existing}
    for row in incoming:result[row[key]]=row
    return list(result.values())
class Page(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.text=[];self.links=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('script','style'):self.skip+=1
        if tag=='a' and attrs.get('href'):self.links.append(attrs['href'])
        if tag in ('br','tr','p','h1','h2','h3','h4','li'):self.text.append('\n')
        if tag in ('td','th'):self.text.append(' | ')
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.skip=max(0,self.skip-1)
        if tag in ('tr','p','h1','h2','h3','h4','li'):self.text.append('\n')
    def handle_data(self,data):
        if not self.skip:self.text.append(data)
    @property
    def plain(self):return '\n'.join(re.sub(r'\s+',' ',x).strip() for x in ''.join(self.text).splitlines() if x.strip())
def parse_html(raw):
    for codec in ['utf-8','cp949']:
        try:decoded=raw.decode(codec);break
        except UnicodeDecodeError:pass
    else:raise ValueError('extract_failed: unknown text encoding')
    p=Page();p.feed(decoded)
    if len(raw)<500 or '채용' not in p.plain:raise ValueError('extract_failed: non-job/empty/block page (even if HTTP 200)')
    return p

def fetch(url):
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname!=ALLOWED_HOST:raise ValueError('Only https://job.alio.go.kr public pages are allowed')
    req=Request(url,headers={'User-Agent':'PublicOfficeJobsSample/0.1 (personal research; one-shot public pages)'})
    with urlopen(req,timeout=20) as r:
        final=r.geturl()
        if urlparse(final).hostname!=ALLOWED_HOST:raise ValueError('Unexpected redirect: refusing to collect')
        raw=r.read(5_000_001)
        if len(raw)>5_000_000:raise ValueError('Response exceeds 5 MB')
        mime=r.headers.get('Content-Type','')
        if 'html' not in mime.lower():raise ValueError('extract_failed: not HTML')
        return raw,final

def discover(p):
    found=[]
    for href in p.links:
        if 'recruitview.do' in href.lower() or 'recruitView.do' in href:
            m=re.search(r'(?:[?&])idx=(\d+)',href)
            if m and m[1] not in found:found.append(m[1])
    if not found:raise ValueError('extract_failed: no recognized detail links; do not report zero new jobs')
    return found

def parse_detail(idx,p,url,raw_path):
    text=p.plain
    for required in ['응시자격','채용인원','전형']:
        if required not in text:raise ValueError('extract_failed: required marker '+required+' absent')
    dates=re.search(r'(?:채용기간|공고기간)\s*\|?\s*((?:20)?\d{2})[.-](\d{2})[.-](\d{2})\s*~\s*((?:20)?\d{2})[.-](\d{2})[.-](\d{2})',text)
    if not dates:raise ValueError('extract_failed: announcement dates absent')
    def date(y,m,d):return datetime.date(int(y) if len(y)==4 else 2000+int(y),int(m),int(d)).isoformat()
    start=date(*dates.groups()[:3]);end=date(*dates.groups()[3:])
    count=re.search(r'채용인원\s*\|?\s*(\d+)\s*명?',text)
    if not count:raise ValueError('extract_failed: announcement total absent')
    return {'id':'job-alio-'+str(idx),'idx':str(idx),'source_url':url,'posted':start,'deadline':end,'announcement_total_headcount':int(count[1]),'body_text':text,'attachment_links':[urljoin(url,x) for x in p.links if 'download' in x.lower()],'raw_path':raw_path,'fetched_at':now(),'collection_method':'python_public_html','review_status':'needs_unit_review','note':'공고 전체 인원. 모집단위 인원·지원요건·급여·경쟁률은 별도 확인 후 reviewed JSON에 반영.'}

def run(limit=5,output=None):
    if not 1<=limit<=10:raise ValueError('limit must be 1..10')
    output=Path(output or ROOT/'data/collected_inbox.json')
    status_path=output.with_name('collection_status.json')
    status={'started_at':now(),'mode':'live_public_html','source':LIST_URL,'status':'running','attempted_requests':0,'collected_records':0,'failure':None}
    active=LIST_URL
    try:
        status['attempted_requests']+=1
        raw,final=fetch(active);page=parse_html(raw);ids=discover(page)[:limit]
        incoming=[]
        for idx in ids:
            time.sleep(1) # one request per second, no automatic retry
            active=f'https://job.alio.go.kr/recruitview.do?idx={idx}'
            status['attempted_requests']+=1
            raw,final=fetch(active);page=parse_html(raw)
            digest=hashlib.sha256(raw).hexdigest()
            dest=ROOT/'evidence/live'/f'{idx}-{digest[:12]}.html';dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
            item=parse_detail(idx,page,final,str(dest.relative_to(ROOT)));item['sha256']=digest;incoming.append(item)
        existing=json.loads(output.read_text(encoding='utf-8')) if output.exists() else []
        merged=upsert(existing,incoming)
        atomic_json(output,merged)
        status.update(status='success_needs_review',collected_records=len(incoming),new_ids=len({x['id'] for x in incoming}-{x['id'] for x in existing}),stored_count=len(merged))
        code=0
    except Exception as e:
        # Commit nothing to inbox on an incomplete run. Preserve last good data.
        status.update(status='failed',failure={'url':active,'type':type(e).__name__,'message':str(e)},data_preserved=True)
        code=1
    status['finished_at']=now();atomic_json(status_path,status)
    print(json.dumps(status,ensure_ascii=False,indent=2));return code
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--limit',type=int,default=5);parser.add_argument('--output',type=Path);args=parser.parse_args();sys.exit(run(args.limit,args.output))
