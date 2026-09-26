# Power-Grid Planning

This case studies a mixed-integer conic power-grid planning problem. Both
systems received the same neutral specification and input identities. The
generated algorithms are preserved under `native_codex/algorithm/` and
`veras/algorithm/`.

| System | Reconstructed objective | Integrality | Audit status |
| --- | ---: | ---: | --- |
| Native Codex raw generated point | 40,726,008.4147 USD/year | Exact | Strict selector ineligible |
| VERAS | **39,379,929.1225 USD/year** | Exact | Independent audit passed |

VERAS improves the reconstructed annualized objective by `1,346,079.2922 USD/year` or
`3.3052%`. The final solve kept all planning variables free, used four solver
threads, consumed `880.278s` under a 900-second optimizer ceiling, used no
external warm start, and had zero external objective discrepancy.

The native comparison is labeled raw because its strict selection report
recorded a maximum positive residual of `4.0663e-6` and a difference between
the rounded reported objective and external reconstruction. The raw objective
is still included for transparent algorithm-level comparison.

The operational input workbook and model data are not redistributed.
