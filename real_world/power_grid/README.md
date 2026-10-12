# Power-Grid Planning

The manuscript features the **selected, independently audited VERAS
solution at USD 39,423,636.2501/year**. A later native Codex run with the
same task, model/effort, public inputs, evaluator, and configured agent cap
scores USD 45,690,260.3341/year, a descriptive **13.715% cross-run
difference**. The selected result was not the contemporaneous VERAS arm of
that later pair.

| Arm | Annualized objective (USD/year) | Clean source replay | Independent audit |
| --- | ---: | ---: | --- |
| Native Codex (later pair) | 45,690,260.3341 | 823.924 s | Pass |
| VERAS (same pair) | 42,613,227.0073 | 746.537 s | Pass |
| **VERAS (selected separate run)** | **39,423,636.2501** | 829.552 s | Pass |

The prospectively launched native--VERAS pair itself shows a **6.735%**
reduction. The 39.424M run uses the same VERAS controller hash and public
inputs but differs in sampling and actual inference time. All three final
artifacts regenerate byte-identically from public-input-only source under a
stricter 750-second replay solver setting; the original agent contract allows
900 seconds and four threads. The paired VERAS generator requires its own
agent-produced `audit.py` dependency alongside `solve.py`; an initial
solve-only replay failed on that missing import, and the dependency-complete
recheck succeeded. Planning decisions remain free and no external solution
warm start is used. See `selected_vs_native_reference.json` for comparison
scope, `paired/` for the later pair, and `veras_separate_audited/` for the
featured solution. Neither VERAS solution is a global optimum certificate.

The older `native_codex/` and `veras/` directories preserve historical
studies, including an older native raw candidate that was strict-selector
ineligible. Those values must not be substituted for the paired rows. The
operational workbook, full NPZ outputs, and MOSEK license are not distributed.
