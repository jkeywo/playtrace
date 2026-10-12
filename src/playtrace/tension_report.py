"""Standalone HTML report for paired tension experiments."""

from __future__ import annotations

import json
from pathlib import Path

from playtrace.tension import ABComparison, TensionBatch


def write_tension_report(
    baseline: TensionBatch,
    candidate: TensionBatch,
    comparison: ABComparison,
    output: Path,
) -> None:
    payload = json.dumps(
        {
            "baseline": baseline.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
            "comparison": comparison.model_dump(mode="json"),
        },
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_document(payload), encoding="utf-8")


def _document(payload: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crisis Wardens — Tension A/B Report</title><style>
:root{{color-scheme:light dark;--bg:light-dark(#f3efe6,#141715);--ink:light-dark(#242720,#eef1e8);--muted:light-dark(#686d63,#aeb5aa);--panel:light-dark(#fffdf7,#20241f);--line:light-dark(#d8d2c5,#3e443c);--a:light-dark(#256f62,#78c6ad);--b:light-dark(#a45b25,#efad74)}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:28px}}h1,h2,p{{margin-top:0}}.muted{{color:var(--muted)}}.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}}.card,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:15px}}.card{{flex:1 1 170px}}.card strong{{display:block;font-size:1.45rem}}.panel{{margin:15px 0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;text-align:right;border-bottom:1px solid var(--line)}}th:first-child,td:first-child{{text-align:left}}.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:12px}}svg{{width:100%;height:210px;overflow:visible}}.axis{{stroke:var(--line);stroke-width:1}}.target{{fill:var(--a);opacity:.09}}.base{{fill:none;stroke:var(--a);stroke-width:3}}.cand{{fill:none;stroke:var(--b);stroke-width:3}}.legend span{{margin-right:16px}}.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}}code{{overflow-wrap:anywhere}}@media(max-width:600px){{main{{padding:16px}}.table-wrap{{overflow:auto}}}}
</style></head><body><main><h1>Crisis Wardens — Tension A/B Report</h1>
<p class="muted">Danger is state-derived. Flow is judged separately against explicit targets: upward trend, meaningful rises and falls, a late climax, and finishing at 70–95 danger.</p>
<div class="cards" id="headline"></div>
<section class="panel"><h2>Results by responder combination</h2><div class="table-wrap"><table><thead><tr><th>Line-up</th><th>Variant</th><th>Win</th><th>Excitement</th><th>Finish danger</th><th>Peak danger</th><th>All targets</th></tr></thead><tbody id="results"></tbody></table></div></section>
<section class="panel"><h2>Mean danger curves</h2><p class="legend"><span><i class="dot" style="background:var(--a)"></i>Baseline</span><span><i class="dot" style="background:var(--b)"></i>Candidate</span><span class="muted">Shaded band = desired finish danger</span></p><div class="charts" id="charts"></div></section>
<section class="panel"><h2>Paired A/B verdict</h2><div id="verdict"></div></section>
<section class="panel"><h2>Heuristic</h2><p>Escalation establishes the minimum danger: Rising 10, Flooding 38, Emergency 68, Catastrophic 100. Threshold saturation contributes 65%, imminent Escalation cards 20%, and fatigue 15% of the safety margin remaining above that floor.</p><p class="muted">Excitement is five equal 20-point tests: useful danger range, positive trend, significant swings and reversals, late peak, and near-loss finish. A meaningful change is 4 points. The target report never modifies a trace.</p></section>
</main><script>
const d={payload}; const combos=d.baseline.combinations.map(x=>x.combination); const label=x=>x.replace('CHR-001','Orra').replace('CHR-002','Senn').replace('+',' + '); const pct=x=>Math.round(x*100)+'%';
document.getElementById('headline').innerHTML=[['Paired runs',d.comparison.paired_runs],['Baseline excitement',d.baseline.mean_excitement],['Candidate delta',(d.comparison.mean_excitement_delta>0?'+':'')+d.comparison.mean_excitement_delta],['Candidate better',pct(d.comparison.candidate_better_rate)],['Baseline parity spread',d.baseline.combination_excitement_spread]].map(x=>`<div class="card"><span class="muted">${{x[0]}}</span><strong>${{x[1]}}</strong></div>`).join('');
function rows(batch,name){{return batch.combinations.map(x=>`<tr><td>${{label(x.combination)}}</td><td>${{name}}</td><td>${{pct(x.win_rate)}}</td><td>${{x.mean_excitement}}</td><td>${{x.mean_finish_danger}}</td><td>${{x.mean_peak_danger}}</td><td>${{pct(x.target_pass_rate)}}</td></tr>`).join('')}}document.getElementById('results').innerHTML=rows(d.baseline,'Baseline')+rows(d.candidate,'Candidate');
function meanCurve(batch,combo){{const runs=batch.analyses.filter(x=>x.combination===combo);const n=21;return Array.from({{length:n}},(_,i)=>{{const p=i/(n-1);return runs.reduce((sum,r)=>{{const j=Math.round(p*(r.samples.length-1));return sum+r.samples[j].danger}},0)/runs.length}})}}
function path(values){{return values.map((v,i)=>`${{i?'L':'M'}} ${{30+i*240/(values.length-1)}} ${{180-v*1.5}}`).join(' ')}}
document.getElementById('charts').innerHTML=combos.map(c=>{{const a=meanCurve(d.baseline,c),b=meanCurve(d.candidate,c);return `<article><h3>${{label(c)}}</h3><svg viewBox="0 0 285 200" role="img" aria-label="Mean danger through game"><rect class="target" x="30" y="37.5" width="240" height="37.5"/><path class="axis" d="M30 30V180H270"/><text x="2" y="34" class="muted">100</text><text x="10" y="108" class="muted">50</text><text x="18" y="184" class="muted">0</text><path class="base" d="${{path(a)}}"/><path class="cand" d="${{path(b)}}"/><text x="30" y="198" class="muted">setup</text><text x="237" y="198" class="muted">finish</text></svg></article>`}}).join('');
const better=d.comparison.mean_excitement_delta>0;document.getElementById('verdict').innerHTML=`<p><strong>${{better?'Candidate improves':'Candidate does not improve'}} the composite flow score.</strong> Mean paired delta: ${{d.comparison.mean_excitement_delta}}; it wins ${{pct(d.comparison.candidate_better_rate)}} of matched runs. Baseline line-up spread is ${{d.comparison.baseline_combination_spread}} versus ${{d.comparison.candidate_combination_spread}} for the candidate.</p><ul>${{d.comparison.deltas.map(x=>`<li>${{label(x.combination)}}: excitement ${{x.excitement_delta>=0?'+':''}}${{x.excitement_delta}}, finish danger ${{x.finish_danger_delta>=0?'+':''}}${{x.finish_danger_delta}}, target pass ${{x.target_pass_rate_delta>=0?'+':''}}${{Math.round(x.target_pass_rate_delta*100)}} points.</li>`).join('')}}</ul>`;
</script></body></html>"""
