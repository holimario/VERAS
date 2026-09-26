# Candidate solution

Run:

```bash
./run_candidate.sh
```

The algorithm builds a deterministic weighted task/table cut from both visible
dates. Fixed-region tables anchor task placement, and coordinate descent uses
the evaluator's exact writer-list endpoint rule. A final resource-repair pass
moves only tasks that cannot affect any visible transfer endpoint, preserving
the optimized bandwidth while adding headroom under all four regional limits.

The generated mapping is written to `results/solution.parquet` with exactly the
columns `task_name, region`.

Verified with the supplied evaluator:

- dispatch time: 39.531 seconds
- worst daily directional bandwidth peak: 1072.295873 Gbps
- mean daily maximum: 940.407674 Gbps
- strict resource feasibility: passed
- largest observed resource utilization: 98.4543%

The full evaluator output is retained in `results/metrics.json`.
