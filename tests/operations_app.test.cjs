const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const app=require('../ui/app.js');
const fixture=()=>JSON.parse(fs.readFileSync(path.join(__dirname,'../data/notices.json'),'utf8'));
const source=fs.readFileSync(path.join(__dirname,'../ui/app.js'),'utf8');

// A small event/element harness, not a browser rendering engine. It exercises the
// production bootstrap, mount, controls and navigation without network/browser use.
function mounted(data,{href='file:///public-office-jobs/index.html',fetcher}={}){
 const nodes=new Map(),events={};
 class Element{
  constructor(id){this.id=id;this.listeners={};this.dataset={};this.textContent='';this.value='';this.classList={toggle(){}};}
  set innerHTML(value){this.html=value;for(const match of value.matchAll(/id="([^"]+)"/g))nodes.set(match[1],new Element(match[1]));}
  get innerHTML(){return this.html||'';}
  addEventListener(type,fn){this.listeners[type]=fn;}
  trigger(type,value){this.value=value;this.listeners[type]?.({target:this});}
  focus(){this.focused=true;}
  querySelectorAll(){return [...this.innerHTML.matchAll(/data-unit="(\d+)"/g)].map(match=>{const el=new Element('unit'+match[1]);el.dataset.unit=match[1];return el;});}
  querySelector(){return new Element('focused');}
 }
 for(const id of ['app','dataset','delivery-status'])nodes.set(id,new Element(id));
 nodes.get('dataset').textContent=JSON.stringify(data);
 const document={getElementById:id=>nodes.get(id),title:''},location={href,hash:''};
 const window={fetch:fetcher||(()=>{throw Error('unexpected network');}),addEventListener:(type,fn)=>events[type]=fn,scrollTo(){}};
 vm.runInNewContext(source,{window,document,location,URL,Date,Intl,Map,Set,JSON,Number,String,Object,Array,Error,RegExp});
 return {nodes,document,window,route(hash){location.hash=hash;events.hashchange();}};
}
const settled=()=>new Promise(resolve=>setImmediate(resolve));

test('HTTP page refresh obtains versioned same-origin published JSON without cache',async()=>{
 const data=fixture(),calls=[];
 const fetcher=async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>data};};
 assert.equal((await app.loadPublished(data,'https://example.github.io/jobs/#/past',fetcher,100)).mode,'published');
 await app.loadPublished(data,'https://example.github.io/jobs/index.html#/current',fetcher,200);
 assert.equal(calls[0].url,'https://example.github.io/jobs/data/notices.json?_=100');
 assert.equal(calls[1].url,'https://example.github.io/jobs/data/notices.json?_=200');
 for(const call of calls){assert.equal(call.options.cache,'no-store');assert.equal(call.options.credentials,'same-origin');}
});
test('File copy remains usable offline and does not call fetch',async()=>{
 const data=fixture();const out=await app.loadPublished(data,'file:///C:/jobs/index.html',()=>{throw Error('must not fetch');});
 assert.equal(out.mode,'file');assert.equal(out.data,data);assert.ok(app.deliveryHTML(out).includes('내려받은 화면'));
});
test('Invalid/error published data preserves embedded records with visible failure',async()=>{
 const data=fixture();for(const response of [{ok:false,status:503},{ok:true,json:async()=>({error:'blocked'})},{ok:true,json:async()=>{throw Error('not JSON');}}]){
  const out=await app.loadPublished(data,'https://example.github.io/jobs/',async()=>response);
  assert.equal(out.mode,'fallback');assert.equal(out.data,data);assert.ok(app.deliveryHTML(out).includes('최신 게시 데이터를 읽지 못했습니다'));
 }
});
test('Published dataset rejects duplicate notices and incomplete unit fields',()=>{
 const data=fixture();data.notices.push(structuredClone(data.notices[0]));assert.throws(()=>app.validatePublished(data));
 const broken=fixture();delete broken.notices[0].units[0].computer;assert.throws(()=>app.validatePublished(broken));
});
test('Bootstrap replaces embedded snapshot with successfully fetched newer data',async()=>{
 const old=fixture(),latest=fixture();latest.notices[0].title='새 게시자료 제목';latest.presentation={generatedAt:'2026-09-13T10:00:00Z'};
 const page=mounted(old,{href:'https://example.github.io/jobs/',fetcher:async()=>({ok:true,json:async()=>latest})});
 await settled();assert.ok(page.nodes.get('rows').innerHTML.includes('새 게시자료 제목'));
 assert.ok(page.nodes.get('delivery-status').innerHTML.includes('서버에 게시된 데이터를 읽었습니다'));
 assert.ok(page.nodes.get('app').innerHTML.includes('개별 공고의 원문 확인 시각이 아닙니다'));
});
test('Production DOM event paths cover search, region, reset, past page and detail',async()=>{
 const data=fixture(),page=mounted(data);await settled();
 const current=app.searchRecords(data,'current',{},data.asOfDate),target=current[0];
 assert.ok(page.nodes.get('rows').innerHTML.includes(target.id));
 page.nodes.get('search').trigger('input',target.organization);
 assert.ok(page.nodes.get('rows').innerHTML.includes(target.id));
 page.nodes.get('region').trigger('change','존재하지 않는 지역');
 assert.ok(page.nodes.get('rows').innerHTML.includes('조건에 맞는 저장 공고가 없습니다'));
 page.nodes.get('reset').trigger('click');assert.ok(page.nodes.get('rows').innerHTML.includes(target.id));
 page.route('#/past');assert.ok(page.document.title.includes('지난 공고'));
 const past=app.searchRecords(data,'past',{},data.asOfDate)[0];assert.ok(page.nodes.get('rows').innerHTML.includes(past.id));
 page.route('#/detail/'+past.id);for(const text of ['업무와 지원요건','근무조건','전형과 제출서류','공식 원문','첨부파일'])assert.ok(page.nodes.get('app').innerHTML.includes(text));
});
test('Dataset missing ratio definitions does not fabricate comparability',()=>{
 const data=fixture();for(const n of data.notices)for(const u of n.units)if(u.competition)u.competition.definitionStatus='unverified';
 assert.deepEqual(app.ratioGroups(data,data.asOfDate),[]);
 assert.ok(app.sortingHelpHTML(data,'past',data.asOfDate).includes('비교 정렬을 활성화하지 않았습니다'));
 assert.ok(app.sortingHelpHTML(data,'current',data.asOfDate).includes('비교기간·표본 수'));
});
test('Factual ratio sort keeps units separate and different definitions out, unknown last',()=>{
 const data=fixture(),n=structuredClone(data.notices.find(n=>app.classify(n,data.asOfDate)==='past'));
 const base=n.units[0],unit=(id,ratio,key,status='confirmed')=>({...structuredClone(base),id,title:id,competition:{published:ratio,definitionStatus:status,definitionKey:key,scope:'recruitment_unit',definition:key,stages:[]}});
 n.units=[unit('high',50,'applicants/final_selected'),unit('low',4,'applicants/final_selected'),unit('other',1,'interview_attendees/final_selected'),unit('unknown',2,'applicants/final_selected','unverified')];
 const set={...data,notices:[n]},group='applicants/final_selected';
 assert.equal(app.ratioGroups(set,data.asOfDate)[0].count,2);
 const rows=app.competitionRows(set,{ratioGroup:group},data.asOfDate);
 assert.deepEqual(rows.map(r=>r.unit.id),['low','high','unknown']);assert.equal(rows.at(-1).value,null);
 assert.deepEqual(app.parseRoute('#/detail/'+n.id+'/low'),{page:'detail',id:n.id,unitId:'low'});
});
test('Ratio sorting DOM can open the exact selected unit',async()=>{
 const data=fixture(),n=data.notices.find(n=>app.classify(n,data.asOfDate)==='past'),u=n.units[0];
 n.units=[{...structuredClone(u),id:'unit-high',title:'높은 단위',competition:{...u.competition,published:50,definitionStatus:'confirmed',definitionKey:'same',definition:'동일한 확인 정의',scope:'recruitment_unit'}},{...structuredClone(u),id:'unit-low',title:'낮은 단위',competition:{...u.competition,published:4,definitionStatus:'confirmed',definitionKey:'same',definition:'동일한 확인 정의',scope:'recruitment_unit'}}];
 const page=mounted(data);await settled();page.route('#/past');page.nodes.get('sort').trigger('change','official_ratio');
 const html=page.nodes.get('rows').innerHTML;assert.ok(html.indexOf('/unit-low')<html.indexOf('/unit-high'));
 page.route('#/detail/'+n.id+'/unit-low');assert.ok(page.document.title.includes('낮은 단위'));
});
test('Past multi-unit row names every displayed ratio instead of treating first as total',()=>{
 const data=fixture(),n=structuredClone(data.notices.find(n=>n.units[0].competition));
 n.units.push({...structuredClone(n.units[0]),id:'second',title:'다른 사무 단위',competition:{...n.units[0].competition,published:7}});
 const html=app.rowHTML(n,'past',data.asOfDate);assert.ok(html.includes('다른 사무 단위'));assert.ok(html.includes('7 : 1'));assert.ok(html.includes(n.units[0].competition.published+' : 1'));
});
test('Partial result refresh visibly labels preserved earlier official ratio',()=>{
 const n={sources:[],autoSync:{resultCheck:{status:'partial',checkedAt:'new-check',issues:[{unitId:'office',note:'표 해석 실패',previousGoodResultPreserved:true}]}}},u={id:'office'},c={published:25,definition:'정의 미확인',stages:[]};
 const html=app.competitionHTML(c,n,u);for(const text of ['new-check','표 해석 실패','기존에 저장된 공식 수치를 보존','25 : 1'])assert.ok(html.includes(text));
});
test('Unmatched result unit is explicitly disconnected from newly received official table',()=>{
 const c={published:8,definition:'정의 미확인',stages:[]},u={id:'office',competition:c};
 const n={sources:[{id:'alio',url:'https://job.alio.go.kr/recruitview.do?idx=300568'}],autoSync:{resultCheck:{status:'partial',checkedAt:'latest-attempt',issues:[],unmatchedUnitIds:['office']}}};
 const html=app.competitionHTML(c,n,u);
 for(const value of ['결과 표 확인 시도: latest-attempt','현재 공식 결과 표와 이 모집단위를 연결하지 못했습니다','이전에 저장한 공식 경쟁률','이번 수신에서 확인된 결과로 해석하지 마세요','공식 결과 표 확인','idx=300568','8 : 1'])assert.ok(html.includes(value));
 const unknown=app.competitionHTML(null,n,{id:'office',competition:null});
 assert.ok(unknown.includes('이 모집단위의 결과는 미확인으로 유지'));assert.ok(!unknown.includes('이전에 저장한 공식 경쟁률'));
 assert.ok(!app.resultCheckHTML(n,{id:'another'}).includes('연결하지 못했습니다'));
});
test('Status summary counts basic fields without mixing notice total or component count',()=>{
 const data=fixture(),counts=app.fieldSummary(data);assert.equal(counts.total,data.notices.reduce((sum,n)=>sum+n.units.length*10,0));
 assert.ok(counts.unverified>0);assert.ok(app.fieldSummaryHTML(data).includes('추출 실패'));
});
test('Saved numeric vacancies keep their partial certainty in list and detail summary',()=>{
 const n=structuredClone(fixture().notices[0]);n.units=[{...n.units[0],vacancies:{value:1,status:'partial'}}];
 assert.equal(app.countUnits(n),1);assert.ok(app.countHTML(n).includes('1명'));assert.ok(app.countHTML(n).includes('일부 확인'));
 assert.ok(app.vacancyHTML(n.units[0].vacancies).includes('badge partial'));
 n.units[0].vacancies.status='confirmed';assert.equal(app.countHTML(n),'1명');assert.equal(app.vacancyHTML(n.units[0].vacancies),'1명');
});
test('Held official notices stay separate from office listings and escape all remote text',()=>{
 const coverage={heldNoticesTotal:21,heldNotices:[{id:'job-alio-999999',organization:'기관<script>',title:'조건<img> 확인',sourceUrl:'https://job.alio.go.kr/recruitview.do?idx=999999',lastFetchedAt:'now<svg>',status:'unverified',reasons:['고용형태<iframe> 구분 미확인']}]};
 const html=app.heldNoticesHTML(coverage);
 for(const value of ['모집단위 정리 대기','21건 중 1건 표시','위 사무직 공고 수에는 포함하지 않았습니다','이후 수집 실행에서 다시 처리합니다','idx=999999','badge unverified'])assert.ok(html.includes(value));
 for(const value of ['<script>','<img>','<svg>','<iframe>'])assert.ok(!html.includes(value));
 coverage.heldNotices[0].sourceUrl='javascript:alert(1)';assert.ok(app.heldNoticesHTML(coverage).includes('href="#"'));
 assert.equal(app.heldNoticesHTML({heldNotices:[]}), '');
});
test('Responsive source retains desktop grid, mobile cards, accessible viewport and labels',()=>{
 const css=fs.readFileSync(path.join(__dirname,'../ui/styles.css'),'utf8'),template=fs.readFileSync(path.join(__dirname,'../ui/template.html'),'utf8');
 assert.ok(css.includes('@media(max-width:820px)'));assert.ok(css.includes('@media(max-width:450px)'));
 assert.ok(css.includes('.table-head{display:none}'));assert.ok(css.includes('content:attr(data-label)'));
 assert.ok(css.includes('.details-grid{display:block}'));assert.ok(css.includes('overflow-wrap:anywhere'));
 assert.ok(template.includes('width=device-width,initial-scale=1'));assert.ok(template.includes('aria-live="polite"'));
 // This is source-level responsive coverage, not a claim of browser layout QA.
});
