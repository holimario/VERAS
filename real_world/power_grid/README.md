# 54-bus distribution network planning

VERAS achieves an annualized cost of **USD 39.424M/year**, reducing cost by **6.28%** relative to Hourly MISOCP and **13.71%** relative to Native Codex.

Annualized costs in million USD/year.

| Method | Total ↓ | Investment ↓ | Operation ↓ |
| --- | ---: | ---: | ---: |
| Hourly MISOCP | 42.065 | 18.984 | 23.081 |
| Varying-timestep MISOCP | 42.923 | 19.012 | 23.911 |
| [Native Codex](native_codex/algorithm/solve.py) | 45.690 | 18.490 | 27.200 |
| [VERAS](veras/algorithm/solve.py) | 39.424 | 16.154 | 23.269 |

## Comparison files

`comparison.json` collects the two methods' metrics, source hashes, and evaluation configuration.

```text
comparison.json
native_codex/algorithm/solve.py
native_codex/evidence/result.json
veras/algorithm/solve.py
veras/evidence/result.json
```

## Running the algorithms

Run the generated solvers in the task's Python environment with the 54-bus input workbook. The solvers write their NPZ solution under `/app/submissions`.

See `../../veras/README.md` for model and runtime configuration.
