# Power-Grid Planning

The revised manuscript reports **VERAS Grid R1** in `veras_v030_transfer/`:
independently reconstructed objective **39,423,636.2501 USD/year**, exact
integrality and all audited constraints feasible. A source-only, public-input
replay with a stricter 750-second solver budget (versus the original agent's
900-second setting) ends in 829.552 seconds and produces a byte-identical NPZ.
The final solve keeps planning free; it uses no external warm start, but does
construct internal starts from public inputs. The input workbook, full NPZ,
and MOSEK license are not distributed.

The original historical run remains archived:

| Historical system | Reconstructed objective | Audit status |
| --- | ---: | --- |
| Native Codex raw (`native_codex/`) | 40,726,008.4147 USD/year | Strict selector ineligible |
| Earlier VERAS (`veras/`) | 39,379,929.1225 USD/year | Independent audit passed |

Grid R1 and the historical native row used different prompts and compute
budgets. The historical 3.3052% native-relative comparison belongs only to
the earlier run; it is **not** a paired effect estimate for Grid R1. Grid R1
is a time-limited feasible solution, not a global optimum certificate.
