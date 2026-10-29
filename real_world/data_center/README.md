# Inter-datacenter placement

VERAS reduces peak inter-datacenter bandwidth by **10.34%** compared with Native Codex.

Peak bandwidth and migration volume.

| Method | Peak (Gbps) ↓ | Migration (PB) ↓ |
| --- | ---: | ---: |
| Operational mapping | 19,495.167 | 0.000 |
| Historical-majority heuristic | 18,276.071 | 0.333 |
| [Native Codex](native_codex/algorithm/solve.py) | 1,238.625 | 25.473 |
| [VERAS](veras/algorithm/solve.py) | 1,110.603 | 25.901 |

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

The dispatchers read task inputs under `/app/data` and generate a placement Parquet file under `/app/submissions`.

See `../../veras/README.md` for model and runtime configuration.
