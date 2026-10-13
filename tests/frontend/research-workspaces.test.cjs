// 独立控制器的异步与交接测试：只用内存 DOM 替身，不访问运行中的账户或平台。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const lists = require('../../app/static/research-lists.js');

function setup(request = async () => ({})) {
  const nodes = new Map(), handlers = {};
  function node(key) {
    if (!nodes.has(key)) nodes.set(key, {innerHTML:'',textContent:'',scrollTop:100,hidden:false,value:'',
      classList:{add(){},remove(){}},setAttribute(){},focus(){},
      querySelector: key => node(key),addEventListener:(name,fn)=>{handlers[name]=fn;}});
    return nodes.get(key);
  }
  const state={userId:'u1',selectedPortfolioId:'p1',activeView:'market',workspacePages:{market:'radar'},workspaceScrollPositions:{}};
  const location={hash:'#sectors/overview'}, drafts=[], urls=[];
  const context=vm.createContext({console,URL,URLSearchParams,Promise,document:{querySelector:node,querySelectorAll:()=>[node('panel')]},
    window:{location,history:{pushState(_state,_title,hash){location.hash=hash;}}}});
  vm.runInContext(fs.readFileSync('app/static/research-workspaces.js','utf8'),context);
  const controller=context.window.FundScopeResearchWorkspaces.create({getState:()=>state,
    request:async url=>{urls.push(url);return request(url);},endpoints:{largeCapital:'/capital',marketHotspots:'/hotspots'},
    icons(){},ask:(...args)=>drafts.push(args),openFund(){},lists,labels:['半导体'],renderHotspot:()=>'<li>证据</li>'});
  controller.bind();
  const click=(selector,dataset={})=>handlers.click({target:{closest:key=>key===selector?{dataset}:null}});
  return {controller,state,location,nodes,urls,drafts,click};
}
test('mount does not read, collect or invoke; radar requests only its two bounded snapshots',async()=>{
  const app=setup(async url=>url.startsWith('/capital')?{data_date:'2026-09-08',topic_signals:[]}:{hotspots:[]});
  assert.equal(app.urls.length,0);
  await app.controller.loadRadar();
  assert.equal(app.urls.length,2); assert.ok(app.urls[1].includes('limit=30'));
  assert.ok(!app.urls.join().includes('sync')); assert.equal(app.drafts.length,0);
});
test('partial provider failure remains explicit while successful evidence survives',async()=>{
  const app=setup(async url=>{if(url.startsWith('/capital'))throw new Error('资金不可用');return {hotspots:[],data_cutoff:'2026-09-08'};});
  await app.controller.loadRadar();
  const html=app.nodes.get('#sector-radar-content').innerHTML;
  assert.match(html,/资金读取失败/); assert.match(html,/2026-09-08/); assert.match(html,/数据不足/);
});
test('late responses for an old portfolio cannot replace the newer radar',async()=>{
  const pending=[];
  const app=setup(url=>new Promise(resolve=>pending.push({url,resolve})));
  const old=app.controller.loadRadar(); app.state.selectedPortfolioId='p2'; const current=app.controller.loadRadar();
  pending[2].resolve({data_date:'NEW',topic_signals:[]}); pending[3].resolve({hotspots:[]}); await current;
  pending[0].resolve({data_date:'OLD',topic_signals:[]}); pending[1].resolve({hotspots:[]}); await old;
  assert.match(app.nodes.get('#sector-radar-content').innerHTML,/NEW/);
  assert.doesNotMatch(app.nodes.get('#sector-radar-content').innerHTML,/OLD/);
});
test('detail creates a deep link and remembers list position; AI handoff is explicit',()=>{
  const app=setup(); app.state.workspacePages.market='news';app.location.hash='#intelligence/events';
  app.state.marketEvents={events:[{event_id:'evt:x',title:'半导体事件',summary:'摘要',event_date:'2026-09-08',source_name:'来源'}]};
  app.click('[data-research-detail]',{researchDetail:'event',detailId:'evt:x'});
  assert.match(app.location.hash,/detail=event&id=evt%3Ax/);
  assert.equal(app.state.workspaceScrollPositions['market/news'],100);
  assert.equal(app.drafts.length,0);
  app.click('[data-detail-ask]');
  assert.equal(app.drafts.length,1); assert.match(app.drafts[0][0],/evt:x/);
  assert.equal(app.urls.length,0);
});
test('topic links stay bound to their stable key after list ordering changes',()=>{
  const app=setup();app.state.workspacePages.market='community';app.location.hash='#community/topics?detail=topic&id=theme%3Ab';
  app.state.socialSignals={aggregates:[{topic_key:'theme:b',topic_label:'第二个话题',evidence_posts:[]},{topic_key:'theme:a',topic_label:'第一个话题',evidence_posts:[]}]};
  app.controller.restore();
  assert.match(app.nodes.get('#research-detail-page').innerHTML,/第二个话题/);
  assert.doesNotMatch(app.nodes.get('#research-detail-page').innerHTML,/第一个话题/);
});
test('a malformed cross-workspace detail cannot replace the current page',()=>{
  const app=setup();app.state.activeView='portfolio';app.location.hash='#assets/holdings?detail=event&id=x';
  app.controller.restore(); assert.equal(app.nodes.get('#research-detail-page').innerHTML,'');
});
