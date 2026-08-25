# Tension measurement and paired experiments

PlayTrace treats danger and dramatic flow as different measurements.

Danger is a 0–100 reading of an observed simulation state. Escalation sets its
floor: Rising 10, Flooding 38, Emergency 68, Catastrophic 100. Tableau cards
contribute according to their distance from a registered threshold. Escalation
cards still in the draw deck and responder fatigue also consume the safety
margin above the escalation floor. Every sample stores all four components.

Flow is measured at setup, every completed turn, and the terminal event. Its
five equally weighted goals are:

- a danger range of at least 35 points;
- a positive linear trend;
- rises, recoveries, and at least two direction reversals of 4+ points;
- a peak in the last 30% of play;
- final danger from 70 through 95: close to loss but not Catastrophic.

`simulate-batch` runs identical seeds for Orra, Senn, and Orra plus Senn. The
result includes per-run samples, per-line-up summaries, win rates, target pass
rates, and spread between line-up excitement means. `compare-batches` matches
results by seed and line-up, so unmatched randomness cannot count as an
improvement. `tension-report` renders a self-contained HTML report.

Experiment overrides remain non-canonical and are included in each run's
configuration and digest. A candidate should not be adopted merely because it
raises final danger: it must preserve recoveries, acceptable win rates, and
similar flow across all line-ups.
