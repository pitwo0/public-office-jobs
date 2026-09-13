#!/usr/bin/env python3
"""Bounded public-source route probe. Retains raw bytes even for failed HTTP responses.
No keys, cookies, browser automation, retries, scheduling, or UI/data updates.
"""
import argparse,datetime,hashlib,json,re,time,urllib.error,urllib.parse,urllib.request
from html.parser import HTMLParser
try:
 from .collect import parse_html, discover, upsert
except ImportError:
 from collect import parse_html, discover, upsert
from pathlib import Path
MAX_REQUESTS=12
MAX_BYTES_PER_RESPONSE=2_000_000
MAX_TOTAL_BYTES=10_000_000
ALLOWED_HOSTS={'job.alio.go.kr','alio.go.kr','www.alio.go.kr','www.data.go.kr'}
ROOT=Path(__file__).resolve().parents[1]

def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds')
def dump(p,o):p.write_text(json.dumps(o,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
class Recorder:
 def __init__(self,out):
  self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True)
  self.log=self.out/'requests.json'
  self.events=json.loads(self.log.read_text(encoding='utf-8')) if self.log.exists() else []
 def fetch(self,url,label,method='GET',body=None):
  p=urllib.parse.urlparse(url)
  if p.scheme!='https' or p.hostname not in ALLOWED_HOSTS:raise ValueError('Only documented official HTTPS origins allowed')
  if not re.fullmatch(r'[a-zA-Z0-9_-]+',label):raise ValueError('Invalid label')
  if len(self.events)>=MAX_REQUESTS:raise RuntimeError('Request budget exhausted')
  if any(e.get('origin')==p.hostname and e.get('stop_origin') for e in self.events):raise RuntimeError('This origin is stopped after an access restriction')
  remaining=MAX_TOTAL_BYTES-sum(e.get('bytes',0) for e in self.events)
  if remaining<=0:raise RuntimeError('Byte budget exhausted')
  n=len(self.events)+1;prefix=f'{n:02d}-{label}'
  e={'sequence':n,'label':label,'origin':p.hostname,'url':url,'method':method,'started_at':utc(),'status':'reserved','request_bytes':len(body or b'')}
  if body is not None:e['request_form']=body.decode('utf-8')
  self.events.append(e);dump(self.log,self.events) # reserve before sending
  raw=b'';t=time.monotonic()
  headers={}
  req_headers={'User-Agent':'PublicOfficeJobsRouteProbe/0.1 (bounded public recruitment research)','Accept':'text/html,application/json;q=0.9,*/*;q=0.5'}
  if body is not None:req_headers['Content-Type']='application/x-www-form-urlencoded; charset=UTF-8'
  req=urllib.request.Request(url,data=body,method=method,headers=req_headers)
  try:
   try:r=urllib.request.build_opener(NoRedirect).open(req,timeout=20)
   except urllib.error.HTTPError as err:r=err
   with r:
    e['http_status']=r.code;e['final_url']=r.geturl();headers=dict(r.headers.items())
    cap=min(MAX_BYTES_PER_RESPONSE,remaining)
    raw=r.read(cap+1)
    if len(raw)>cap:raw=raw[:cap];e['truncated']=True
    e['status']='received' if 200<=r.code<300 else 'http_error'
  except Exception as exc:e.update(status='transport_error',error_type=type(exc).__name__,error=str(exc))
  e.update(finished_at=utc(),elapsed_seconds=round(time.monotonic()-t,3),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),body_file=prefix+'.body')
  # Public response headers only. Discard cookie values; they are not needed as evidence.
  headers={k:('[redacted]' if k.lower() in ('set-cookie','authorization','proxy-authorization') else v) for k,v in headers.items()}
  dump(self.out/(prefix+'.headers.json'),headers)
  e['headers_file']=prefix+'.headers.json'
  (self.out/e['body_file']).write_bytes(raw)
  preview=raw[:5000].decode('utf-8','replace')
  if re.search(r'verify you are human|automated (?:traffic|queries)|access denied|접근.{0,8}(?:차단|제한)|허용되지 않은|ROBOTS_DENIED|BLOCK_LIST',preview,re.I):e['stop_origin']=True
  if e.get('http_status') in (401,403,429):e['stop_origin']=True
  e['content_type']=next((v for k,v in headers.items() if k.lower()=='content-type'),None)
  dump(self.log,self.events)
  return raw,e

class Node:
 def __init__(self,tag='',attrs=()):self.tag=tag;self.attrs=dict(attrs);self.children=[]
 def all(self,tag=None):
  for c in self.children:
   if isinstance(c,Node):
    if tag is None or c.tag==tag:yield c
    yield from c.all(tag)
 def text(self):
  if self.tag in ('script','style'):return ''
  if self.tag=='br':return '\n'
  return ''.join(c.text() if isinstance(c,Node) else c for c in self.children)
 def clean(self):return re.sub(r'\s+',' ',self.text()).strip()
class Tree(HTMLParser):
 def __init__(self,raw):
  super().__init__(convert_charrefs=True);self.root=Node();self.stack=[self.root];self.feed(raw.decode('utf-8'))
 def handle_starttag(self,tag,attrs):
  n=Node(tag,attrs);self.stack[-1].children.append(n)
  if tag not in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):self.stack.append(n)
 def handle_startendtag(self,tag,attrs):
  self.handle_starttag(tag,attrs)
  if self.stack[-1].tag==tag:self.stack.pop()
 def handle_endtag(self,tag):
  for i in range(len(self.stack)-1,0,-1):
   if self.stack[i].tag==tag:del self.stack[i:];break
 def handle_data(self,data):self.stack[-1].children.append(data)
def first(nodes,message):
 try:return next(iter(nodes))
 except StopIteration:raise ValueError('extract_failed: '+message)
def cells(row):return [c for c in row.children if isinstance(c,Node) and c.tag in ('th','td')]
def parse_list(raw,url,expected_page):
 legacy=discover(parse_html(raw)) # reuse the existing v0.1 link parser
 tree=Tree(raw).root
 page=first((n for n in tree.all('input') if n.attrs.get('name')=='pageNo'),'pageNo absent').attrs.get('value')
 if page!=str(expected_page):raise ValueError('extract_failed: pageNo mismatch')
 rows=[]
 for tr in tree.all('tr'):
  links=[n.attrs.get('href','') for n in tr.all('a') if re.search(r'/recruitview\.do\?idx=\d+',n.attrs.get('href',''),re.I)]
  if not links:continue
  cs=cells(tr)
  if len(cs)!=9:raise ValueError('extract_failed: list columns changed')
  idx=re.search(r'idx=(\d+)',links[0])[1]
  if not cs[2].clean() or not cs[3].clean() or not re.fullmatch(r'20\d{2}\.\d{2}\.\d{2}',cs[6].clean()):raise ValueError('extract_failed: invalid notice row')
  rows.append({'id':'job-alio-'+idx,'idx':idx,'title':cs[2].clean(),'org':cs[3].clean(),'region_raw':cs[4].clean(),'employment_raw':cs[5].clean(),'registered_raw':cs[6].clean(),'closing_raw':cs[7].clean(),'application_period':{'raw':None,'status':'미확인','note':'목록의 등록일/마감일을 접수기간으로 바꾸지 않음'},'source_url':urllib.parse.urljoin(url,links[0]),'list_page':expected_page,'collection_method':'python_public_html','review_status':'transport_sample_not_office_unit_reviewed'})
 if not rows or [r['idx'] for r in rows]!=legacy:raise ValueError('extract_failed: row IDs disagree with legacy link parser')
 if len({r['idx'] for r in rows})!=len(rows):raise ValueError('extract_failed: duplicate list IDs')
 return {'page':expected_page,'rows':rows,'observed_go_pages':sorted(set(int(x) for x in re.findall(r'goPage\((\d+)\)',raw.decode('utf-8'))))}
def parse_detail(raw,url,expected=None):
 plain=parse_html(raw).plain;tree=Tree(raw).root
 title=first((n for n in tree.all('p') if 'titleH2' in n.attrs.get('class','').split()),'detail title absent').clean()
 top=first((n for n in tree.all('div') if 'topInfo' in n.attrs.get('class','').split()),'notice header absent')
 org=first(top.all('h2'),'institution absent').clean()
 idx=urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get('idx',[None])[0]
 if not idx or not idx.isdigit():raise ValueError('extract_failed: invalid detail ID')
 if expected and (idx!=expected['idx'] or org!=expected['org'] or title!=expected['title']):raise ValueError('extract_failed: list/detail identity mismatch')
 fields={}
 metadata=first((n for n in tree.all('div') if 'detailTxt' in n.attrs.get('class','').split()),'announcement metadata absent')
 for tr in metadata.all('tr'):
  cs=cells(tr)
  for k in range(len(cs)-1):
   if cs[k].tag=='th' and cs[k+1].tag=='td':fields[cs[k].clean()]=cs[k+1].clean()
 for label in ('채용인원','채용기간','등록일','고용형태','근무지'):
  if not fields.get(label):raise ValueError('extract_failed: detail field absent: '+label)
 tab=first((n for n in tree.all('div') if n.attrs.get('id')=='tab-1'),'detail body absent')
 sections={};heading=None
 for c in tab.children:
  if not isinstance(c,Node):continue
  if c.tag=='h4':heading=c.clean()
  elif c.tag=='p' and heading:sections[heading]=c.text().strip()
 if not all(sections.get(k) for k in ('응시자격','전형절차/방법')):raise ValueError('extract_failed: recruitment conditions absent')
 # Only explicitly labelled application-period lines; no substitution of announcement dates.
 period=[line.strip() for line in sections['전형절차/방법'].splitlines() if re.search(r'접수\s*기간\s*[:：]',line) and '~' in line and re.search(r'\d',line)]
 info=first((n for n in tree.all('p') if 'infoLink' in n.attrs.get('class','').split()),'source URL section absent')
 originals=[n.attrs['href'] for n in info.all('a') if urllib.parse.urlparse(n.attrs.get('href','')).scheme in ('http','https')]
 units=[]
 for table in tree.all('table'):
  names=[n.clean() for n in table.all('th') if 'interviewName' in n.attrs.get('class','').split()]
  if not names:continue
  unit_rows=[[c.clean() for c in cells(tr)] for tr in table.all('tr')]
  ratio=re.search(r'최종\s*경쟁률\s*(\d+(?:\.\d+)?)\s*대\s*1(?:\s|$)',table.clean())
  ratio_rows=[row for row in unit_rows if any('경쟁률' in cell for cell in row)]
  status='확인' if ratio else ('추출 실패' if ratio_rows else '미공개')
  units.append({'unit_name_raw':names[0],'official_ratio':{'label':'최종 경쟁률','value_raw':ratio[1] if ratio else None,'status':status,'definition_status':'미확인','note':'해당 표의 공식 표기. 비율 행 부재는 이 표에 한정한 미공개; 행은 있지만 형식 미인식이면 추출 실패. 산식 추정·단계별 비율 계산 없음'},'table_rows_raw':unit_rows})
 return {'id':'job-alio-'+idx,'idx':idx,'org':org,'title':title,'source_url':url,'institution_notice_urls':originals,'announcement_period_raw':fields['채용기간'],'application_period':{'raw':'\n'.join(period) or None,'status':'확인' if period else '미확인','source_section':'전형절차/방법','note':'명시된 접수기간 원문만 추출; 날짜 정규화·모집단위별 상이한 기간 검토는 별도'},'announcement_fields_raw':fields,'sections':sections,'unit_results_raw':units,'attachment_urls':[n.attrs['href'] for n in tree.all('a') if 'download' in n.attrs.get('href','').lower()],'collection_method':'python_public_html','review_status':'needs_unit_review'}
def checked_fetch(rec,url,label,parser,*args,**kwargs):
 raw,event=rec.fetch(url,label,**kwargs)
 try:
  if event['status']!='received' or event.get('truncated') or event.get('stop_origin') or 'html' not in (event.get('content_type') or '').lower():raise ValueError('fetch_failed: no complete accepted HTML response')
  value=parser(raw,url,*args);event['content_validation']='valid_recruitment_content'
 except Exception as exc:
  event.update(content_validation='failed',validation_error=str(exc));dump(rec.log,rec.events);raise
 dump(rec.log,rec.events)
 value['evidence']={'sequence':event['sequence'],'raw_file':event['body_file'],'sha256':event['sha256'],'received_at':event['finished_at']}
 dump(rec.out/(Path(event['body_file']).stem+'.parsed.json'),value)
 return value
def merge_rows(existing,incoming):
 old={r['id']:r for r in existing}
 return upsert(existing,[dict(old.get(r['id'],{}),**r) for r in incoming])
def verify_route(out,rounds=2,detail_id=None):
 rec=Recorder(out);receipt={'started_at':utc(),'method':'python_public_html','rounds_requested':rounds,'status':'running','rounds':[]}
 target=None;inbox=rec.out/'probe_inbox.json'
 try:
  if len(rec.events)+3*rounds>MAX_REQUESTS:raise RuntimeError('Insufficient remaining request budget')
  for number in range(1,rounds+1):
   one=checked_fetch(rec,'https://job.alio.go.kr/recruit.do?replacement=Y',f'round{number}_list1',parse_list,1)
   if 2 not in one['observed_go_pages']:raise ValueError('extract_failed: page 2 navigation absent')
   two=checked_fetch(rec,'https://job.alio.go.kr/recruit.do',f'round{number}_list2',parse_list,2,method='POST',body=b'pageNo=2&replacement=Y&order=REG_DATE&sort=DESC')
   if {r['idx'] for r in one['rows']}=={r['idx'] for r in two['rows']}:raise ValueError('extract_failed: pagination returned identical IDs')
   candidates=one['rows']+two['rows']
   chosen=target or detail_id
   item=first((r for r in candidates if not chosen or r['idx']==chosen),'requested detail ID not present in observed list pages')
   target=item['idx']
   detail=checked_fetch(rec,item['source_url'],f'round{number}_detail',parse_detail,item)
   merged=merge_rows(json.loads(inbox.read_text(encoding='utf-8')) if inbox.exists() else [],candidates)
   merged=upsert(merged,[dict(item,detail=detail)])
   dump(inbox,merged) # complete round only; original dashboard data never touched
   receipt['rounds'].append({'round':number,'list1_ids':[r['idx'] for r in one['rows']],'list2_ids':[r['idx'] for r in two['rows']],'detail_id':target,'detail_application_period_status':detail['application_period']['status'],'stored_count':len(merged),'unique_ids':len({r['id'] for r in merged})})
  receipt['status']='success'
 except Exception as exc:receipt.update(status='failed',error_type=type(exc).__name__,error=str(exc),last_complete_inbox_preserved=True)
 receipt['finished_at']=utc();dump(rec.out/f'verification_run_{len(rec.events):02d}.json',receipt);print(json.dumps(receipt,ensure_ascii=False,indent=2));return 0 if receipt['status']=='success' else 1

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--url');p.add_argument('--label',default='request');p.add_argument('--form',help='URL encoded form only; no secrets');p.add_argument('--verify',action='store_true');p.add_argument('--rounds',type=int,choices=(1,2),default=2);p.add_argument('--detail-id');a=p.parse_args()
 if a.verify:return verify_route(a.out,a.rounds,a.detail_id)
 if not a.url:p.error('--url required')
 _,e=Recorder(a.out).fetch(a.url,a.label,'POST' if a.form is not None else 'GET',a.form.encode() if a.form is not None else None)
 print(json.dumps(e,ensure_ascii=False,indent=2))
 return 0 if e['status']=='received' and not e.get('truncated') and not e.get('stop_origin') else 1
if __name__=='__main__':raise SystemExit(main())
