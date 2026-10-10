# Decision confidence and timing evidence

Operation and target questions remain in one TypeSafe request. Only the selected
operation's target head affects execution. Decisions record operation and target
confidence and their top-to-second probability margins. Executed action history
preserves those fields so a wrong operation can be distinguished from a wrong target.

Until thresholds have been calibrated on browser tasks, only numerical ties
(margin <= 0.000001) prevent automatic actions. The chat discards the decision,
observes again and chooses once more. A second tie before progress pauses with a
technical explanation, without requesting manual control or generating field text.
The library executor also rejects tied action decisions before input. DONE still
requires independent verification; BLOCKED still requires current-page assessment.
Confidence is not authorization and never bypasses external-action approval.

Detailed timing responses include `latency_samples` for each stage, with
`sample_count`, `p50_ms` (median) and `p95_ms` (nearest rank). These describe the
retained events, capped at 1,000, rather than all historical requests. Compact
polling retains its existing totals and does not calculate percentiles. Stages
overlap: do not add their durations to estimate end-to-end latency.

For comparisons, collect complete turns including routing, safety, blockers and
verification. Report end-to-end time separately from approval/manual waiting,
success, false success, unnecessary intervention, provider requests and tokens.
Token counts do not establish billed cost. Compare the same goals and page
fixtures before selecting confidence thresholds or changing auxiliary models.
No speed or quality improvement is claimed without those measurements.
