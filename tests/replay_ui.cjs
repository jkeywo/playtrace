const {test} = require('node:test');
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(require('node:path').join(__dirname, '../src/playtrace/report_assets/plugin.js'), 'utf8');
const ui = require('node:vm').runInThisContext(source + ';PlayTraceReplay');
const state = (data, turn = 1) => ({kind:'plugin',turn,status:'running',data});
test('card gallery includes indirect effects, removed cards and hands without duplicates', () => {
  const before=state({card_names:{a:{id:'one'},b:{id:'two'},c:{id:'three'}},locations:{x:{cards:{a:{value:2},b:{value:1}},discard:[]}},players:{p:{hand:[]}}});
  const after=structuredClone(before);
  after.data.locations.x.cards.a.value=4; delete after.data.locations.x.cards.b;
  after.data.locations.x.discard=['b']; after.data.players.p.hand=['c'];
  assert.deepEqual(new Set(ui.involvedCards({entity_ids:['one','one']},before,after)),new Set(['one','two','three']));
  assert.deepEqual(ui.involvedCards({entity_ids:[]},before,before),[]);
  assert.deepEqual(ui.involvedCards({entity_ids:[]},state({players:{p:{hand:'custom schema'}}}),state({players:{p:{hand:{a:1}}}})),[]);
});
test('image views only accept embedded raster images', () => {
  assert.ok(ui.safeImage('data:image/webp;base64,YWJj'));
  for(const url of ['javascript:alert(1)','https://example.org/image.png','data:image/svg+xml;base64,YWJj','data:image/png;base64,<img>']) assert.equal(ui.safeImage(url),false);
});
test('movement highlights both locations and the player, including zero-capability players', () => {
  const data = {players:{p:{name:'Explorer', at:'a',hand:[]}},locations:{a:{name:'Hill'},b:{name:'Lake'}}};
  const before = state(data), after = structuredClone(before); after.data.players.p.at = 'b';
  const changed = ui.changesFor(before,after).filter(t=>t.changes.length).map(t=>t.id);
  assert.deepEqual(changed,['players:p','locations:a','locations:b']);
  assert.equal(ui.changeText(ui.changesFor(before,after)[1].changes[0],ui.names(data)),'Hill → Lake');
});
test('tracks, resources, removals and ordered decks retain exact before/after state', () => {
  const before=state({locations:{a:{position:1,deck:['one','two'],cards:{one:{value:3}},resources:{ore:2}}}});
  const after=structuredClone(before); after.data.locations.a.position=2; after.data.locations.a.resources.ore=0; delete after.data.locations.a.cards.one;
  const changes=ui.changesFor(before,after).find(t=>t.id==='locations:a').changes;
  assert.equal(changes.length,3);
  assert.equal(ui.changeText(changes[0],new Map()),'1 → 2 (+1)');
  assert.equal(ui.changeText(changes[2],new Map()),'2 → 0 (-2)');
  assert.deepEqual(changes[1].path,['cards','one']);
  assert.deepEqual(ui.tabs(after)[1].value.deck,['one','two']);
});
test('array changes preserve quantities and detect pure reordering', () => {
  assert.equal(ui.changeText({before:['a','a'],after:['a','b']},new Map([['a','Amber'],['b','Blue']])),'Added Blue · Removed Amber');
  assert.equal(ui.changeText({before:['a','b'],after:['b','a']},new Map()),'Order changed');
});
test('metadata order and no-op actions do not cause false changes; first state is not an action delta', () => {
  assert.deepEqual(ui.diff({a:1,b:2},{b:2,a:1}),[]);
  assert.deepEqual(ui.changesFor(null,state({score:3}))[0].changes,[]);
  assert.deepEqual(ui.changesFor(state({score:3}),state({score:3}))[0].changes,[]);
});
test('unknown state and missing checkpoints are inspectable without invented deltas', () => {
  assert.equal(ui.tabs(state({inventory:['x']}))[0].value.inventory[0],'x');
  assert.ok(ui.changesFor(state({score:1}),null).every(t=>!t.changes.length));
  const before=state({players:{p:{name:'Explorer'}}});
  const removed=ui.changesFor(before,state({players:{}})).find(t=>t.id==='players:p');
  assert.equal(removed.value,undefined); assert.equal(removed.changes.length,1);
});
test('game descriptions and hostile strings are returned as text, with readable fallback', () => {
  const text='</script><img src=x onerror=alert(1)>';
  assert.equal(ui.description({data:{description:text}}),text);
  assert.equal(ui.description({event_type:'custom.action',data:{}}),'Custom Action.');
  assert.equal(ui.display('card-id',ui.names({card_names:{a:{id:'card-id',name:'Sample card'}}})),'Sample card');
});
