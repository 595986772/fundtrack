// 产品收敛后的导航和列表契约；测试不采集社媒、不调用真实模型。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const pages = require('../../app/static/workspace-pages.js');
const lists = require('../../app/static/research-lists.js');

test('exactly five product entries, each with at most three primary views', () => {
  assert.deepEqual(Object.keys(pages.destinations), ['assets', 'intelligence', 'community', 'sectors', 'research']);
  for (const [id, destination] of Object.entries(pages.destinations)) {
    assert.ok(destination.pages.length <= 3);
    assert.deepEqual(pages.readRoute(`#${id}`), pages.destinationRoute(id));
  }
});
test('old URLs map to canonical destinations without losing their business owner', () => {
  const cases = [
    ['#overview/holdings', '#assets/fund/holdings'], ['#portfolio/stress', '#assets/stress'],
    ['#portfolio/research', '#research/reports'], ['#portfolio/decisions', '#research/records'],
    ['#market/hotspots', '#intelligence/trends'], ['#market/community', '#community/topics'],
    ['#market/capital', '#sectors/capital'], ['#agent', '#research/chat'],
  ];
  for (const [old, canonical] of cases) {
    const route = pages.readRoute(old);
    assert.equal(pages.routeHash(route.view, route.page), canonical);
    assert.deepEqual(pages.readRoute(canonical), route);
  }
  assert.deepEqual(pages.readRoute('#intelligence/events?detail=event&id=abc'), {view:'market', page:'news'});
  assert.deepEqual(pages.readRoute('#assets/fund/holdings'), {view:'overview', page:'holdings'});
});
test('auxiliary tools and records do not reappear in main asset navigation', () => {
  assert.deepEqual(pages.destinations.assets.pages.map(row => row[0]), ['holdings','review']);
  for (const page of ['stress','optimizer','transactions']) assert.equal(pages.describe('portfolio',page).primary,false);
  assert.equal(pages.describe('portfolio','research').destination,'research');
});
test('event list escapes evidence and exposes detail, not an expanded report', () => {
  const html=lists.events([{event_id:'x"',title:'<script>bad</script>',summary:'摘要',event_date:'2026-09-08',semantic_analysis:{status:'accepted',themes:['半导体']},source_name:'来源',source_url:'https://example.test'}]);
  assert.match(html,/&lt;script&gt;/); assert.match(html,/data-research-detail="event"/);
  assert.match(html,/半导体/);
  assert.doesNotMatch(html,/<script>|已有 AI 影响解释/);
  const detail=lists.eventDetail({event_id:'x',title:'事件',event_date:'2026-09-08',source_url:'javascript:alert(1)',source_name:'危险来源',affected_fund_codes:['008888']});
  assert.doesNotMatch(detail,/href="javascript:/); assert.match(detail,/data-research-fund="008888"/);
});
test('time filter never manufactures events outside the loaded sample', () => {
  const html=lists.events([{event_id:'old',title:'旧消息',event_date:'2020-01-01'}],{windowDays:'7',now:Date.parse('2026-09-08')});
  assert.match(html,/没有符合时间范围/); assert.doesNotMatch(html,/旧消息/);
  // 发布时间与事件发生日不同，近日报导应按 API 的 published_date 筛选。
  const recent=lists.events([{event_id:'new',title:'最新报道',event_date:'2020-01-01',published_date:'2026-09-08',semantic_analysis:{status:'abstained',themes:['不应当成已确认主题']}}],{windowDays:'1',now:Date.parse('2026-09-08T12:00:00+08:00')});
  assert.match(recent,/最新报道/); assert.match(recent,/AI 暂不判断/);
  assert.doesNotMatch(recent,/不应当成已确认主题/);
});
test('community topic identity uses stable keys, not list index or author totals across topics', () => {
  const html=lists.topics([{topic_key:'theme:semiconductor',topic_label:'半导体',author_count:2,action_counts:{add:1,sell:1}}]);
  assert.match(html,/data-detail-id="theme:semiconductor"/); assert.match(html,/2 位独立作者（该话题内）/);
});
test('sector evidence associates only matching labels and does not invent returns or recommendation scores', () => {
  const rows=lists.sectorRows({topic_signals:[{topic_kind:'sector',topic_code:'chip',topic_label:'半导体',flow_5d_cny:0}]},{hotspots:[{title:'消息',themes:['半导体']},{title:'半导体字样但未分类',themes:[]}]},['医药']);
  const chip=rows.find(row=>row.code==='chip');
  assert.equal(chip.events.length,1);
  const html=lists.radar(rows);
  assert.match(html,/0\.00 亿/); assert.match(html,/未接入/); assert.match(html,/待研究/);
  assert.doesNotMatch(html,/推荐分|0\.00%/);
  assert.match(lists.radar(rows,{query:'不存在'}),/没有符合条件/);
});
test('desktop and mobile expose the same five entry names', () => {
  const html=fs.readFileSync('app/static/index.html','utf8');
  for(const key of Object.keys(pages.destinations)) assert.equal((html.match(new RegExp(`data-product-view="${key}"`,'g'))||[]).length,2);
  for(const key of ['overview','portfolio','market','agent']) assert.ok(!html.includes(`data-product-view="${key}"`));
  for(const id of ['social-posts','social-review','social-settings','radar']) assert.equal((html.match(new RegExp(`data-workspace-page="${id}"`,'g'))||[]).length,1);
});
