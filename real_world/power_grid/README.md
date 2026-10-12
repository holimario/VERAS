# Power-Grid Planning

The main table in the manuscript reports a prospectively launched single-run
native Codex--VERAS pair on the same 54-bus instance. Independent constraint
and objective replay accepts both final artifacts:

| Paired arm | Annualized objective (USD/year) | End-to-end clean source replay | Strict audit |
| --- | ---: | ---: | --- |
| Native Codex | 45,690,260.3341 | 823.924 s | Pass |
| VERAS | 42,613,227.0073 | 746.537 s | Pass |

The paired VERAS result is **6.735% lower** than native. Both use the same
public input and evaluator, GPT-5.6 Sol/xhigh, 15,360-second allowed agent
cap, four solver threads, and 900-second agent solver contract. The stronger
750-second-solver replays regenerate their NPZs byte-identically. For VERAS,
`solve.py` imports a second agent-produced source, `audit.py`; the first
solve-only replay failed on this missing dependency, and a dependency-complete
recheck succeeded. Both programs keep planning decisions free and use no
external solution warm start. Their source and sanitized evidence are under
`paired/`.

A **separate** audited VERAS run (`veras_separate_audited/`) returns
39,423,636.2501 USD/year and replays byte-identically in 829.552 seconds
with a 750-second solver setting. Its 5.122% solver gap means the result is
feasible, not proven optimal. It is not a member of the native--VERAS pair.

The older `native_codex/` and `veras/` directories preserve historical
studies, including an older native raw candidate that was strict-selector
ineligible. Those values must not be substituted for the paired rows. The
operational workbook, full NPZ outputs, and MOSEK license are not distributed.
