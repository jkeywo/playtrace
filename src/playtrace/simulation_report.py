"""Self-contained interactive reports for deterministic simulation traces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from playtrace.simulation import SimulationRun


def write_simulation_report(runs: Iterable[SimulationRun], output: Path) -> None:
    traces = [_report_trace(run) for run in runs]
    if not traces:
        raise ValueError("at least one simulation run is required")
    data = json.dumps(traces, separators=(",", ":"), ensure_ascii=True).replace(
        "<", "\\u003c"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    renderer = _plugin_document if any(t["plugin_state"] for t in traces) else _document
    output.write_text(renderer(data), encoding="utf-8")


def write_simulation_fragment(runs: Iterable[SimulationRun], output: Path) -> None:
    traces = [_report_trace(run) for run in runs]
    if not traces:
        raise ValueError("at least one simulation run is required")
    data = json.dumps(traces, separators=(",", ":"), ensure_ascii=True).replace(
        "<", "\\u003c"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    renderer = _plugin_fragment if any(t["plugin_state"] for t in traces) else _fragment
    output.write_text(renderer(data), encoding="utf-8")


def _report_trace(run: SimulationRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "plugin_state": any(getattr(c.state, "kind", None) == "plugin" for c in run.state_trace),
        "plugin_id": run.provenance.plugin_id,
        "label": run.config.id,
        "characters": list(run.config.character_ids),
        "ruleset": run.config.ruleset_id,
        "seed": run.config.seed,
        "status": run.final_state.status.value if run.final_state else "blocked",
        "metrics": run.metrics.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in run.events],
        "states": [checkpoint.state.model_dump(mode="json") for checkpoint in run.state_trace],
        "blockers": [blocker.model_dump(mode="json") for blocker in run.blockers],
    }


def _plugin_document(data: str) -> str:
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>PlayTrace simulation replay</title><body>'
            + _plugin_fragment(data) + '</body></html>')


def _plugin_fragment(data: str) -> str:
    # Provider strings enter the DOM through textContent only.
    return '''<main id="pt-plugin-report">
<style>#pt-plugin-report{font:16px/1.5 system-ui;max-width:1200px;margin:auto;padding:24px}
#pt-plugin-report pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef4f5;color:#142830;padding:16px}
#pt-plugin-report .panels{display:grid;grid-template-columns:1fr 1fr;gap:20px}
#pt-plugin-report input{width:65%}@media(max-width:700px){#pt-plugin-report .panels{display:block}}</style>
<h1>PlayTrace simulation replay</h1><label>Trace <select id="pt-trace"></select></label>
<p id="pt-summary"></p><button id="pt-prev">Previous</button>
<input id="pt-position" type="range" min="0" value="0" aria-label="Event position">
<button id="pt-next">Next</button><p id="pt-counter"></p>
<div class="panels"><section><h2>Event and source references</h2><pre id="pt-event"></pre>
<h2>Blockers</h2><pre id="pt-blockers"></pre></section>
<section><h2>Complete state checkpoint</h2><pre id="pt-state"></pre></section></div>
<script>(()=>{const traces=DATA;const root=document.getElementById('pt-plugin-report');
const q=id=>root.querySelector('#pt-'+id);let ti=0,ei=0;
traces.forEach((t,i)=>{const o=document.createElement('option');o.value=i;o.textContent=t.label;q('trace').append(o)});
function render(){const t=traces[ti];q('position').max=Math.max(0,t.events.length-1);q('position').value=ei;
q('summary').textContent=t.plugin_id+' · '+t.status+' · seed '+t.seed+' · '+t.metrics.turns_completed+' turns';
q('counter').textContent=t.events.length?'Event '+(ei+1)+' / '+t.events.length:'No events';
q('event').textContent=JSON.stringify(t.events[ei]||{},null,2);
q('state').textContent=JSON.stringify(t.states[ei]||{},null,2);
q('blockers').textContent=JSON.stringify(t.blockers,null,2)}
q('trace').onchange=e=>{ti=Number(e.target.value);ei=0;render()};
q('position').oninput=e=>{ei=Number(e.target.value);render()};
q('prev').onclick=()=>{ei=Math.max(0,ei-1);render()};
q('next').onclick=()=>{ei=Math.min(Math.max(0,traces[ti].events.length-1),ei+1);render()};render()})();</script></main>'''.replace('DATA', data)


def _document(data: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crisis Wardens — Simulation Report</title>
<style>
:root {{ color-scheme: light dark; --bg: light-dark(#f4f0e7,#141715); --ink: light-dark(#242720,#eef1e8); --muted: light-dark(#686d63,#aeb5aa); --panel: light-dark(#fffdf7,#20241f); --line: light-dark(#d8d2c5,#3e443c); --accent: light-dark(#256f62,#78c6ad); --danger: light-dark(#a33b36,#ef8b82); --warn: light-dark(#8a6415,#e4bd61); --soft: light-dark(#e8e2d5,#30362f); }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.45 system-ui,sans-serif; }}
main {{ max-width: 1280px; margin: auto; padding: 28px; }}
h1,h2,h3,p {{ margin-top: 0; }} h1 {{ margin-bottom: 6px; }} h2 {{ font-size: 1.05rem; }}
.muted {{ color: var(--muted); }}
.toolbar,.transport,.stats,.workspace,.state-grid,.player-grid {{ display: flex; gap: 12px; flex-wrap: wrap; }}
.toolbar {{ align-items: end; margin: 22px 0; }}
label {{ display: grid; gap: 5px; color: var(--muted); }} select,button,input {{ font: inherit; }}
select,button {{ color: var(--ink); background: var(--panel); border: 1px solid var(--line); border-radius: 7px; padding: 8px 11px; }}
button {{ cursor: pointer; }} button:hover {{ border-color: var(--accent); }}
.stats {{ margin-bottom: 18px; }} .stat {{ min-width: 145px; padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 9px; }}
.stat strong {{ display: block; font-size: 1.35rem; font-weight: 600; }}
.status-won {{ color: var(--accent); }} .status-lost,.status-blocked {{ color: var(--danger); }}
.timeline {{ margin: 18px 0 24px; }} .transport {{ align-items: center; }}
#scrubber {{ flex: 1 1 360px; accent-color: var(--accent); }}
.timeline-strip {{ display:grid;gap:1px;height:22px;margin-top:9px; }}
.tick {{ min-width:0;border:0;border-radius:2px;padding:0;background:var(--muted);opacity:.4; }}
.tick.player {{ background: var(--accent); }} .tick.scenario {{ background: var(--warn); }} .tick.terminal,.tick.blocker {{ background: var(--danger); }} .tick.active {{ opacity: 1; transform: scaleY(1.35); }}
.workspace {{ align-items: stretch; }} .state {{ flex: 2 1 620px; }} .log-panel {{ flex: 1 1 330px; }}
.panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }}
.event-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }}
.event-data {{ margin: 10px 0 18px; padding: 10px 12px; background: var(--soft); border-radius: 7px; overflow-wrap: anywhere; }}
.state-grid > section {{ flex: 1 1 260px; }}
.tableau {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(155px,1fr)); gap: 8px; }}
.tableau article,.player {{ padding: 10px; background: var(--soft); border-radius: 7px; }}
.cube {{ font-size: 1.45rem; font-weight: 600; color: var(--accent); }}
.player-grid {{ margin-top: 12px; }} .player {{ flex: 1 1 260px; }}
.tokens {{ display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }} .token {{ background: var(--panel); border: 1px solid var(--line); border-radius: 999px; padding: 2px 7px; font-size: .82rem; }}
.log {{ margin: 0; padding: 0; list-style: none; max-height: 720px; overflow: auto; }}
.log button {{ width: 100%; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; text-align: left; background: transparent; padding: 9px 6px; }}
.log button.current {{ background: var(--soft); color: var(--accent); }} .log small {{ display: block; color: var(--muted); }}
@media (max-width: 680px) {{ main {{ padding: 16px; }} .workspace {{ display: block; }} .log-panel {{ margin-top: 14px; }} .log {{ max-height: 360px; }} }}
</style>
</head>
<body>
<main id="report">
  <h1>Crisis Wardens — Simulation Report</h1>
  <p class="muted" id="provenance"></p>
  <div class="toolbar"><label>Trace<select id="trace"></select></label></div>
  <div class="stats" id="stats"></div>
  <section class="timeline" aria-label="Simulation timeline">
    <div class="transport">
      <button type="button" id="previous">Previous</button>
      <button type="button" id="play">Play</button>
      <button type="button" id="next">Next</button>
      <input id="scrubber" type="range" min="0" value="0" aria-label="Event position">
      <span id="position" aria-live="polite"></span>
    </div>
    <div class="timeline-strip" id="timeline"></div>
  </section>
  <div class="workspace">
    <section class="state panel" aria-live="polite">
      <div class="event-head"><h2 id="event-title"></h2><span class="muted" id="turn"></span></div>
      <div class="event-data" id="event-data"></div>
      <div class="state-grid">
        <section><h3>Scenario</h3><div id="scenario"></div></section>
        <section><h3>Tableau</h3><div class="tableau" id="tableau"></div></section>
      </div>
      <h3>Responders</h3><div class="player-grid" id="players"></div>
    </section>
    <aside class="log-panel panel"><h2>Action log</h2><ol class="log" id="log"></ol></aside>
  </div>
</main>
<script>
const traces={data};
const names={{"CHR-001":"Orra","CHR-002":"Senn","SCD-001":"Swollen River","SCD-002":"Bridge Under Strain","SCD-003":"Families Cut Off","SCD-004":"Panicked Evacuation","SCD-005":"Heavy Rain","SCD-006":"River Breaks the Bank","SCD-007":"Debris Strike","SCD-008":"Bolting Herd","SCD-009":"Frightened Livestock","SCD-010":"Calls for Help","SCD-011":"Sudden Calm","SCD-012":"Escalation"}};
const labels={{"simulation.started":"Simulation started","scenario.setup":"Scenario set up","turn.started":"Turn started","turn.completed":"Turn completed","player.card_activated":"Card activated","player.card_action_resolved":"Card action resolved","player.card_discarded":"Card discarded","player.cards_drawn":"Cards drawn","player.rested":"Rest","player.fatigued":"Fatigue","player.ap_spent":"AP spent","player.ap_lost":"Unused AP lost","player.power_used":"Power used","player.power_prepared":"Power prepared","assist.played":"Assist played","scenario.card_drawn":"Scenario card drawn","scenario.card_resolved":"Scenario card resolved","scenario.escalated":"Escalation advanced","card.improved":"Card improved","card.worsened":"Card worsened","card.worsen_prevented":"Worsen prevented","card.threshold_suppressed":"Threshold suppressed","tableau.card_added":"Tableau card added","tableau.card_collapsed":"Tableau card collapsed","scenario.deck_reordered":"Scenario deck reordered","effect.no_legal_target":"No legal target","objective.completed":"Victory","objective.failed":"Catastrophic loss","simulation.blocked":"Simulation blocked"}};
const el=id=>document.getElementById(id); let traceIndex=0,eventIndex=0,timer=null;
function displayName(id){{return names[id]||id}}
function eventClass(type){{if(type.startsWith('player')||type.startsWith('assist'))return 'player';if(type.startsWith('objective'))return 'terminal';if(type.includes('blocked'))return 'blocker';if(type.startsWith('scenario')||type.startsWith('card')||type.startsWith('tableau'))return 'scenario';return ''}}
function tokens(items){{return `<div class="tokens">${{items.map(x=>`<span class="token">${{displayName(x)}}</span>`).join('')}}</div>`}}
function setTrace(index){{traceIndex=Number(index);eventIndex=0;stop();build();render()}}
function build(){{const t=traces[traceIndex];el('provenance').textContent=`${{t.ruleset}} · seed ${{t.seed}} · ${{t.characters.map(displayName).join(' + ')}}`;el('scrubber').max=Math.max(0,t.events.length-1);el('stats').innerHTML=[['Result',t.status],['Turns',t.metrics.turns_completed],['Actions',t.metrics.player_actions],['Assists',t.metrics.assists]].map(([a,b])=>`<div class="stat"><span class="muted">${{a}}</span><strong class="${{a==='Result'?'status-'+b:''}}">${{b}}</strong></div>`).join('');el('timeline').style.gridTemplateColumns=`repeat(${{t.events.length}},minmax(0,1fr))`;el('timeline').innerHTML=t.events.map((e,i)=>`<button type="button" class="tick ${{eventClass(e.event_type)}}" data-i="${{i}}" aria-label="Event ${{i+1}}: ${{labels[e.event_type]||e.event_type}}"></button>`).join('');el('timeline').querySelectorAll('button').forEach(b=>b.onclick=()=>{{eventIndex=Number(b.dataset.i);render()}});el('log').innerHTML=t.events.map((e,i)=>`<li><button type="button" data-i="${{i}}"><strong>${{labels[e.event_type]||e.event_type}}</strong><small>Turn ${{e.turn}} · ${{e.actor_id?displayName(e.actor_id):e.entity_ids.map(displayName).join(', ')}}</small></button></li>`).join('');el('log').querySelectorAll('button').forEach(b=>b.onclick=()=>{{eventIndex=Number(b.dataset.i);render()}})}}
function render(){{const t=traces[traceIndex],e=t.events[eventIndex],s=t.states[eventIndex];if(!e||!s)return;el('scrubber').value=eventIndex;el('position').textContent=`${{eventIndex+1}} / ${{t.events.length}}`;el('event-title').textContent=labels[e.event_type]||e.event_type;el('turn').textContent=`Turn ${{s.turn}} · ${{s.status}}`;const detail=[];if(e.actor_id)detail.push(`Actor: ${{displayName(e.actor_id)}}`);if(e.entity_ids.length)detail.push(`Affected: ${{e.entity_ids.map(displayName).join(', ')}}`);for(const [k,v] of Object.entries(e.data))detail.push(`${{k.replaceAll('_',' ')}}: ${{typeof v==='object'?JSON.stringify(v):v}}`);el('event-data').textContent=detail.join(' · ')||'State checkpoint';el('scenario').innerHTML=`<p><strong>${{s.escalation}}</strong> escalation</p><p>Scenario deck: ${{s.deck.length}} · discard: ${{s.discard.length}}</p>${{tokens(s.deck.slice(0,5))}}`;el('tableau').innerHTML=Object.entries(s.tableau).map(([id,cubes])=>`<article><strong>${{displayName(id)}}</strong><div class="cube">${{cubes}}</div><span class="muted">${{s.cube_meanings[id]||'cubes'}}</span></article>`).join('')||'<p class="muted">No tableau cards.</p>';el('players').innerHTML=Object.entries(s.players).map(([id,p])=>`<article class="player"><strong>${{displayName(id)}}</strong><p>Hand ${{p.hand.length}} · deck ${{p.deck.length}} · discard ${{p.discard.length}}</p>${{tokens(p.hand)}}<p class="muted">Elements: ${{Object.entries(p.elements).map(([k,v])=>`${{k}} ×${{v}}`).join(', ')||'none'}} · Fatigue ${{p.fatigue_discarded}} · Rests ${{p.rests}}</p></article>`).join('');document.querySelectorAll('.tick').forEach((x,i)=>x.classList.toggle('active',i===eventIndex));document.querySelectorAll('#log button').forEach((x,i)=>x.classList.toggle('current',i===eventIndex));document.querySelector('#log button.current')?.scrollIntoView({{block:'nearest'}})}}
function move(delta){{eventIndex=Math.max(0,Math.min(traces[traceIndex].events.length-1,eventIndex+delta));render()}}
function stop(){{if(timer)clearInterval(timer);timer=null;el('play').textContent='Play'}}
el('trace').innerHTML=traces.map((t,i)=>`<option value="${{i}}">${{t.characters.map(displayName).join(' + ')}} — ${{t.status}}</option>`).join('');el('trace').onchange=e=>setTrace(e.target.value);el('scrubber').oninput=e=>{{eventIndex=Number(e.target.value);render()}};el('previous').onclick=()=>move(-1);el('next').onclick=()=>move(1);el('play').onclick=()=>{{if(timer){{stop();return}};el('play').textContent='Pause';timer=setInterval(()=>{{if(eventIndex>=traces[traceIndex].events.length-1)stop();else move(1)}},450)}};document.addEventListener('keydown',e=>{{if(e.key==='ArrowLeft')move(-1);if(e.key==='ArrowRight')move(1)}});build();render();
</script>
</body>
</html>
"""


def _fragment(data: str) -> str:
    return f"""<div id="cw-sim-viz">
<style>
#cw-sim-viz {{ color:var(--foreground); }}
#cw-sim-viz .cw-controls,#cw-sim-viz .cw-transport,#cw-sim-viz .cw-layout,#cw-sim-viz .cw-state-grid,#cw-sim-viz .cw-players {{ display:flex;gap:12px;flex-wrap:wrap; }}
#cw-sim-viz .cw-controls {{ align-items:end;margin:14px 0; }}
#cw-sim-viz .cw-transport {{ align-items:center; }}
#cw-sim-viz #cw-range {{ flex:1 1 300px; }}
#cw-sim-viz .cw-strip {{ display:grid;gap:1px;height:18px;margin:8px 0 16px; }}
#cw-sim-viz .cw-tick {{ min-width:0;border:0;padding:0;background:var(--muted-foreground);opacity:.35; }}
#cw-sim-viz .cw-tick.cw-player {{ background:var(--viz-series-1); }}
#cw-sim-viz .cw-tick.cw-scenario {{ background:var(--viz-series-2); }}
#cw-sim-viz .cw-tick.cw-terminal {{ background:var(--destructive); }}
#cw-sim-viz .cw-tick.is-selected {{ opacity:1;transform:scaleY(1.3); }}
#cw-sim-viz .cw-main {{ flex:2 1 480px; }}
#cw-sim-viz .cw-log {{ flex:1 1 260px; }}
#cw-sim-viz .cw-event {{ margin-bottom:12px; }}
#cw-sim-viz .cw-state-grid>section {{ flex:1 1 220px; }}
#cw-sim-viz .cw-tableau {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px; }}
#cw-sim-viz .cw-tableau article {{ padding:8px;background:var(--muted);color:var(--muted-foreground); }}
#cw-sim-viz .cw-tableau strong {{ display:block;color:var(--foreground); }}
#cw-sim-viz .cw-cubes {{ color:var(--viz-series-1);font-size:1.35em; }}
#cw-sim-viz .cw-players {{ margin-top:10px; }}
#cw-sim-viz .cw-player-state {{ flex:1 1 220px;padding-top:8px;border-top:1px solid var(--border); }}
#cw-sim-viz .cw-log ol {{ margin:0;padding-left:22px; }}
#cw-sim-viz .cw-log li {{ margin:5px 0; }}
#cw-sim-viz .cw-log .is-selected {{ color:var(--viz-series-1);font-weight:500; }}
@media(max-width:620px){{#cw-sim-viz .cw-layout{{display:block}}#cw-sim-viz .cw-log{{margin-top:14px}}}}
</style>
<h2>Crisis Wardens trace</h2>
<div class="cw-controls viz-controls"><label class="form-label">Trace<select class="form-select" id="cw-trace"></select></label></div>
<div class="cw-transport"><button class="btn" type="button" id="cw-prev">Previous</button><button class="btn btn-primary" type="button" id="cw-play">Play</button><button class="btn" type="button" id="cw-next">Next</button><input class="form-range" id="cw-range" type="range" min="0" value="0" aria-label="Event position"><span id="cw-pos" aria-live="polite"></span></div>
<div class="cw-strip" id="cw-strip"></div>
<div class="cw-layout">
  <section class="cw-main card" aria-live="polite">
    <div class="cw-event"><strong id="cw-event"></strong><div class="text-small text-muted" id="cw-detail"></div></div>
    <div class="cw-state-grid"><section><h3>Scenario</h3><div id="cw-scenario"></div></section><section><h3>Tableau</h3><div class="cw-tableau" id="cw-tableau"></div></section></div>
    <h3>Responders</h3><div class="cw-players" id="cw-players"></div>
  </section>
  <section class="cw-log"><h3>Action log</h3><ol id="cw-log"></ol></section>
</div>
<script>
(()=>{{const root=document.getElementById('cw-sim-viz'),traces={data},q=id=>root.querySelector('#'+id);const names={{'CHR-001':'Orra','CHR-002':'Senn','SCD-001':'Swollen River','SCD-002':'Bridge Under Strain','SCD-003':'Families Cut Off','SCD-004':'Panicked Evacuation','SCD-005':'Heavy Rain','SCD-006':'River Breaks the Bank','SCD-007':'Debris Strike','SCD-008':'Bolting Herd','SCD-009':'Frightened Livestock','SCD-010':'Calls for Help','SCD-011':'Sudden Calm','SCD-012':'Escalation'}};const labels={{'simulation.started':'Simulation started','scenario.setup':'Scenario setup','turn.started':'Turn started','turn.completed':'Turn completed','player.card_activated':'Card activated','player.card_action_resolved':'Card action','player.card_discarded':'Card discarded','player.cards_drawn':'Cards drawn','player.rested':'Rest','player.ap_spent':'AP spent','player.ap_lost':'AP lost','player.power_used':'Power used','player.power_prepared':'Power prepared','assist.played':'Assist','scenario.card_drawn':'Scenario draw','scenario.card_resolved':'Scenario resolved','scenario.escalated':'Escalation','card.improved':'Improve','card.worsened':'Worsen','card.worsen_prevented':'Worsen prevented','card.threshold_suppressed':'Threshold suppressed','tableau.card_added':'Tableau added','objective.completed':'Victory','objective.failed':'Catastrophic loss'}};let ti=0,ei=0,timer=null;const name=x=>names[x]||x;const cls=t=>t.startsWith('player')||t.startsWith('assist')?'cw-player':t.startsWith('objective')?'cw-terminal':t.startsWith('scenario')||t.startsWith('card')||t.startsWith('tableau')?'cw-scenario':'';
function build(){{const t=traces[ti];q('cw-range').max=t.events.length-1;q('cw-strip').style.gridTemplateColumns=`repeat(${{t.events.length}},minmax(0,1fr))`;q('cw-strip').innerHTML=t.events.map((e,i)=>`<button type="button" class="cw-tick ${{cls(e.event_type)}}" data-i="${{i}}" aria-label="${{labels[e.event_type]||e.event_type}}"></button>`).join('');q('cw-strip').querySelectorAll('button').forEach(b=>b.onclick=()=>{{ei=+b.dataset.i;render()}})}}
function render(){{const t=traces[ti],e=t.events[ei],s=t.states[ei];q('cw-range').value=ei;q('cw-pos').textContent=`${{ei+1}} / ${{t.events.length}}`;q('cw-event').textContent=`Turn ${{s.turn}} · ${{labels[e.event_type]||e.event_type}}`;q('cw-detail').textContent=[e.actor_id&&`Actor: ${{name(e.actor_id)}}`,e.entity_ids.length&&`Affected: ${{e.entity_ids.map(name).join(', ')}}`,...Object.entries(e.data).map(([k,v])=>`${{k.replaceAll('_',' ')}}: ${{typeof v==='object'?JSON.stringify(v):v}}`)].filter(Boolean).join(' · ');q('cw-scenario').innerHTML=`<strong>${{s.escalation}}</strong><p class="text-small text-muted">${{s.status}} · deck ${{s.deck.length}} · discard ${{s.discard.length}}</p>`;q('cw-tableau').innerHTML=Object.entries(s.tableau).map(([id,n])=>`<article><strong>${{name(id)}}</strong><span class="cw-cubes">${{n}}</span> ${{s.cube_meanings[id]||'cubes'}}</article>`).join('');q('cw-players').innerHTML=Object.entries(s.players).map(([id,p])=>`<div class="cw-player-state"><strong>${{name(id)}}</strong><div class="text-small">hand ${{p.hand.length}} · deck ${{p.deck.length}} · discard ${{p.discard.length}}</div><div class="text-small text-muted">${{Object.entries(p.elements).map(([k,v])=>`${{k}} ×${{v}}`).join(', ')||'no elements'}} · fatigue ${{p.fatigue_discarded}} · rests ${{p.rests}}</div></div>`).join('');const lo=Math.max(0,ei-4),hi=Math.min(t.events.length,ei+5);q('cw-log').innerHTML=t.events.slice(lo,hi).map((x,j)=>`<li class="${{lo+j===ei?'is-selected':''}}">Turn ${{x.turn}} · ${{labels[x.event_type]||x.event_type}}</li>`).join('');q('cw-strip').querySelectorAll('button').forEach((b,i)=>b.classList.toggle('is-selected',i===ei))}}
function move(n){{ei=Math.max(0,Math.min(traces[ti].events.length-1,ei+n));render()}}function stop(){{if(timer)clearInterval(timer);timer=null;q('cw-play').textContent='Play'}}q('cw-trace').innerHTML=traces.map((t,i)=>`<option value="${{i}}">${{t.characters.map(name).join(' + ')}} — ${{t.status}}</option>`).join('');q('cw-trace').onchange=e=>{{ti=+e.target.value;ei=0;stop();build();render()}};q('cw-range').oninput=e=>{{ei=+e.target.value;render()}};q('cw-prev').onclick=()=>move(-1);q('cw-next').onclick=()=>move(1);q('cw-play').onclick=()=>{{if(timer){{stop();return}}q('cw-play').textContent='Pause';timer=setInterval(()=>ei<traces[ti].events.length-1?move(1):stop(),450)}};build();render()}})();
</script>
</div>
"""
