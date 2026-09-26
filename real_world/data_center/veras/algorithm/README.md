# Temporally robust task-to-region optimizer

The final candidate is strictly resource-feasible on August 8 and August 9. Exact joint replay reports a worst daily max-direction peak of `1072.2334446474997` Gbps, a mean daily peak of `932.23864087575` Gbps, and a temporal spread of `279.9896075434996` Gbps. The final mapping contains 625,191 unique task identifiers, including the single null identifier, with 41 tasks assigned to `怀来`.

## Method

The optimizer first constructs the smallest deterministic resource cover for every visible all-中卫 overload timestamp. Scarcity-normalized deficits across both dates act as resource shadow prices. It then builds compact task/table incidence arrays and repeatedly:

1. replays both dates and directions with starts before ends;
2. extracts the tasks influencing each active bandwidth peak;
3. tests deterministic single moves and writer/consumer star neighborhoods;
4. accepts only strict lexicographic improvements in worst-day peak, mean peak, and spread;
5. rejects any move that breaks either day's resources or raises the August 9 daily peak by more than 0.5%.

The star neighborhoods are compensated hypergraph moves: a writer can move together with high-impact consumers so that an apparent single-move regression is evaluated in its repaired state. Every accepted candidate is checked by full replay; influence scores only nominate moves.

## Reproduce

```bash
cd /work/app
BANDWIDTH_DATA_DIR=/work/app/data candidate/run_candidate.sh
```

The latest clean entry-point dispatch time is `36.000951104` seconds. The entry point uses only the six visible Parquet inputs and writes `candidate/results/solution.parquet`.

Exact validation:

```bash
POLARS_MAX_THREADS=1 BANDWIDTH_DATA_DIR=/work/app/data \
  python evaluate_solution.py candidate/results/solution.parquet \
  --dates 20250808,20250809 \
  --dispatch-seconds "$(tr -d '\n' < candidate/results/dispatch_seconds.txt)" \
  --output candidate/results/metrics.json
```

Separate exact August 8 and August 9 outputs are retained as `results/metrics_20250808.json` and `results/metrics_20250809.json`.

## Preserved feasible pool

| Label | Worst Gbps | Mean Gbps | Spread Gbps | Migration bytes |
|---|---:|---:|---:|---:|
| `seed_cover` | 9086.172629 | 8452.942449 | 1266.460361 | 25,457,508,680,253,748 |
| `robust_peak` | 2658.904935 | 2472.978251 | 371.853369 | 25,919,019,518,787,264 |
| `final_local` | 1072.233445 | 932.238641 | 279.989608 | 25,985,765,545,171,932 |

Migration is retained only as an auxiliary Pareto dimension. No August 10 data, final-holdout feedback, reference task assignment, prior candidate, or external task-by-task mapping was used.
