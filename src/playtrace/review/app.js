const escapeText = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#39;");

const empty = (message) => `<p class="empty">${escapeText(message)}</p>`;

const evidenceMarkup = (links) => links.length
  ? `<ul class="evidence-list">${links.map((link) => `
      <li>
        <span class="relation relation-${escapeText(link.relation)}">${escapeText(link.relation)}</span>
        <div><strong>${escapeText(link.evidence_id)}</strong><p>${escapeText(link.rationale)}</p></div>
      </li>`).join("")}</ul>`
  : empty("No evidence links.");

function phase2Markup(payload) {
  const { snapshot, analysis, review } = payload;
  const proposals = (payload.proposals ?? []).map((record) => {
    const proposal = record.proposal ?? record;
    const attempts = record.attempts ?? [];
    const latest = attempts.at(-1);
    return `<article class="argument proposal proposal-${escapeText(proposal.state)}">
      <p class="eyebrow">DESIGN PROPOSAL / ${escapeText(proposal.state)}</p>
      <h3>${escapeText(proposal.title)}</h3>
      <p>${escapeText(proposal.question)}</p>
      <small>Basis ${escapeText(proposal.basis_digest)} · ${escapeText(proposal.id)}</small>
      ${latest ? `<p><strong>Latest application:</strong> ${escapeText(latest.status)} — ${escapeText(latest.message || "")}</p>` : ""}
    </article>`;
  }).join("");
  const regressionRuns = (payload.regression_runs ?? []).map((run) => `
    <li class="regression-${run.passed ? "passed" : "failed"}">
      <strong>${escapeText(run.suite_id)}</strong>
      <span>${run.passed ? "passed" : "failed"}</span>
      <small>${escapeText(run.digest)}</small>
    </li>`).join("");
  const extensions = (payload.extension_proposals ?? []).map((item) => `
    <li><strong>${escapeText(item.title)}</strong><p>${escapeText(item.kind)} · inert review artefact</p></li>`).join("");
  const sagas = (payload.write_sagas ?? []).map((saga) => `
    <li class="saga-${escapeText(saga.status)}"><strong>${escapeText(saga.id)}</strong>
      <p>${escapeText(saga.status)} · ${saga.operations?.filter((item) => item.verified).length ?? 0}/${saga.operations?.length ?? 0} verified</p>
      ${(saga.failures ?? []).map((failure) => `<small>${escapeText(failure)}</small>`).join("")}
    </li>`).join("");
  const criticRuns = (payload.critic_runs ?? []).map((run) => `
    <li><strong>${escapeText(run.critic_id)}</strong>
      <p>${run.completed ? "completed" : "executor unavailable"} · ${run.attempts?.length ?? 0} attempt(s)</p>
      ${(run.findings ?? []).map((finding) => `<p><strong>${escapeText(finding.title)}</strong> — ${escapeText(finding.statement)}</p>`).join("")}
      <small>${escapeText(run.task_digest)}</small></li>`).join("");
  const decision = review.decision ?? { state: "pending" };
  const experiments = (review.experiment_evidence ?? []).map((item) => `
    <article class="argument experiment experiment-${escapeText(item.applicability)}">
      <p class="eyebrow">EXPERIMENT / ${escapeText(item.kind)}</p>
      <h3>${escapeText(item.summary)}</h3>
      <p><strong>Hypothesis:</strong> ${escapeText(item.hypothesis_link.hypothesis_id)}</p>
      <p><strong>Relation:</strong> ${escapeText(item.hypothesis_link.relation)}</p>
      <span class="applicability applicability-${escapeText(item.applicability)}">${escapeText(item.applicability)}</span>
      ${(item.applicability_reasons ?? []).map((reason) => `<p>${escapeText(reason)}</p>`).join("")}
    </article>`).join("");
  const coverage = review.coverage.items.length
    ? review.coverage.items.map((item) => `
      <li class="coverage-${escapeText(item.status)}">
        <span>${escapeText(item.status)}</span>
        <div><strong>${escapeText(item.target_id)}</strong><p>${escapeText(item.reason)}</p>
        <small>${escapeText(item.covered_by.join(", ") || "not examined")}</small></div>
      </li>`).join("")
    : `<li><span>covered</span><div><strong>No mandatory targets</strong></div></li>`;
  const claims = review.claims.length
    ? review.claims.map((claim) => `
      <article class="argument" id="${escapeText(claim.id)}">
        <p class="eyebrow">CLAIM / ${escapeText(claim.id)}</p>
        <h3>${escapeText(claim.statement)}</h3>
        ${evidenceMarkup(claim.evidence)}
      </article>`).join("")
    : empty("No material claims were generated.");
  const hypotheses = review.hypotheses.length
    ? review.hypotheses.map((hypothesis) => `
      <article class="argument hypothesis">
        <p class="eyebrow">HYPOTHESIS / ${escapeText(hypothesis.id)}</p>
        <h3>${escapeText(hypothesis.statement)}</h3>
        <p><strong>Test:</strong> ${escapeText(hypothesis.test_method)}</p>
        <p><strong>Needs:</strong> ${escapeText(hypothesis.evidence_requirements.join(", ") || "inspectable review")}</p>
      </article>`).join("")
    : empty("No consequential hypothesis was generated.");
  const findings = review.critic_findings.map((finding) => `
    <li class="finding finding-${escapeText(finding.stance)}">
      <p class="eyebrow">${escapeText(finding.critic_id)} / ${escapeText(finding.stance)}</p>
      <strong>${escapeText(finding.title)}</strong>
      <p>${escapeText(finding.statement)}</p>
      ${finding.unsupported_judgment ? '<span class="judgment">UNSUPPORTED JUDGMENT</span>' : ""}
      ${finding.claim_ids.length ? `<small>Claims: ${escapeText(finding.claim_ids.join(", "))}</small>` : ""}
    </li>`).join("");
  const policies = review.policies.map((policy) => `
    <li><span class="severity severity-${escapeText(policy.severity)}">${escapeText(policy.severity)}</span>
    <div><strong>${escapeText(policy.title)}</strong><p>${escapeText(policy.statement)}</p>
    <small>${escapeText(policy.layer)} · ${escapeText(policy.id)}</small></div></li>`).join("");
  const routes = review.critic_routes.map((route) => `
    <li><strong>${escapeText(route.critic_id)}</strong><p>${escapeText(route.reason)}</p></li>`).join("");

  return `
    <section class="metrics" aria-label="Review summary">
      <article><span>Entities</span><strong>${analysis.entity_count}</strong></article>
      <article><span>Claims</span><strong>${review.claims.length}</strong></article>
      <article><span>Coverage</span><strong>${review.coverage.complete ? "complete" : "gaps"}</strong></article>
      <article><span>Critics</span><strong>${review.critic_routes.length}</strong></article>
    </section>
    <section class="panel intent">
      <p class="eyebrow">INTENT / ${escapeText(review.intent.kind)}</p>
      <h2>${escapeText(review.intent.desired_outcome || review.intent.original_prompt)}</h2>
      <p>${escapeText(review.intent.original_prompt)}</p>
      <small>Scope: ${escapeText(review.intent.scope.join(", ") || "whole snapshot")}</small>
    </section>
    <section class="panel decision decision-${escapeText(decision.state)}">
      <p class="eyebrow">HUMAN DECISION</p>
      <h2>${escapeText(decision.state)}</h2>
      <p>${escapeText(decision.rationale || "No authority decision has been recorded. Canon remains unchanged.")}</p>
      ${decision.decided_by ? `<small>${escapeText(decision.decided_by)} · ${escapeText(decision.decided_at)}</small>` : ""}
    </section>
    <div class="columns">
      <section class="panel"><p class="eyebrow">MANDATORY COVERAGE</p><ul class="coverage-list">${coverage}</ul></section>
      <section class="panel"><p class="eyebrow">LAYERED POLICY</p><ul class="knowledge-list">${policies || "<li>No applicable policy.</li>"}</ul></section>
    </div>
    <section class="panel"><p class="eyebrow">CLAIM-LINKED ARGUMENT</p><div class="argument-grid">${claims}</div></section>
    <section class="panel"><p class="eyebrow">HYPOTHESES</p><div class="argument-grid">${hypotheses}</div></section>
    <section class="panel"><p class="eyebrow">EXPERIMENT EVIDENCE</p><div class="argument-grid">${experiments || empty("No experiment evidence was included.")}</div></section>
    <section class="panel"><p class="eyebrow">DESIGN PROPOSALS / READ ONLY</p><div class="argument-grid">${proposals || empty("No design proposals were linked.")}</div></section>
    <div class="columns">
      <section class="panel"><p class="eyebrow">BEHAVIORAL REGRESSIONS</p><ul class="knowledge-list">${regressionRuns || "<li>No regression runs were linked.</li>"}</ul></section>
      <section class="panel"><p class="eyebrow">INERT EXTENSION PROPOSALS</p><ul class="knowledge-list">${extensions || "<li>No extension proposals were linked.</li>"}</ul></section>
    </div>
    <div class="columns">
      <section class="panel"><p class="eyebrow">CANONICAL WRITE SAGAS</p><ul class="knowledge-list">${sagas || "<li>No write saga has been recorded.</li>"}</ul></section>
      <section class="panel"><p class="eyebrow">MODEL CRITIC RUNS</p><ul class="knowledge-list">${criticRuns || "<li>No executor-backed critic run has been recorded.</li>"}</ul></section>
    </div>
    <div class="columns">
      <section class="panel"><p class="eyebrow">CRITIC FINDINGS</p><ul class="findings">${findings || "<li>No findings.</li>"}</ul></section>
      <section class="panel"><p class="eyebrow">CRITIC ROUTING</p><ul class="routes">${routes}</ul></section>
    </div>
    <section class="panel"><p class="eyebrow">ALTERNATIVES</p><ol>${review.alternatives.map((item) => `<li>${escapeText(item)}</li>`).join("")}</ol></section>
    <section class="panel limitations"><p class="eyebrow">LIMITATIONS</p><ul>${review.limitations.map((item) => `<li>${escapeText(item)}</li>`).join("")}</ul></section>
    <footer><span>Snapshot</span><code>${escapeText(snapshot.id)}</code><span>Semantic digest</span><code>${escapeText(analysis.semantic_digest)}</code></footer>`;
}

function legacyMarkup(payload) {
  const { snapshot, analysis, claim, hypothesis } = payload;
  return `<section class="metrics"><article><span>Entities</span><strong>${analysis.entity_count}</strong></article><article><span>Assertions</span><strong>${analysis.assertion_count}</strong></article><article><span>Adapter</span><strong>${escapeText(snapshot.adapter_id)}</strong></article></section>
    <section class="panel"><p class="eyebrow">CLAIM / ${escapeText(claim.id)}</p><h2>${escapeText(claim.statement)}</h2>${evidenceMarkup(claim.evidence)}</section>
    <section class="panel hypothesis"><p class="eyebrow">HYPOTHESIS / ${escapeText(hypothesis.id)}</p><h2>${escapeText(hypothesis.statement)}</h2><p>${escapeText(hypothesis.test_method)}</p></section>`;
}

export function reviewMarkup(payload) {
  if (payload.review) return phase2Markup(payload);
  if (payload.claim && payload.hypothesis) return legacyMarkup(payload);
  return empty("No stored design review is available yet.");
}

export async function loadReview(fetcher = fetch) {
  const response = await fetcher("/api/review");
  if (!response.ok) throw new Error(`Review request failed: ${response.status}`);
  return response.json();
}

if (typeof document !== "undefined") {
  loadReview()
    .then((payload) => {
      document.querySelector("#project-summary").textContent =
        payload.snapshot
          ? `${payload.snapshot.project_id} · ${payload.snapshot.source_revisions.length} source revisions`
          : `${payload.project_id ?? "Play Trace"} · no stored review`;
      document.querySelector("#review").innerHTML = reviewMarkup(payload);
    })
    .catch((error) => {
      document.querySelector("#project-summary").textContent = "Review unavailable";
      document.querySelector("#review").textContent = error.message;
    });
}
