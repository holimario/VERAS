# VERAS Results

This English-language report presents the three main result tables from the
[VERAS paper](https://arxiv.org/abs/2610.24329), together with results for all
107 ORAgentBench tasks and their execution trajectories.

- Open `index.html` through a local HTTP server for the interactive report.
- Machine-readable scores for all tasks are in
  `../../evidence/official-score-summary.json`.
- Execution trajectories are linked from task rows and stored under `tasks/`.
- Paper tables are stored in `../../evidence/paper-results.json`. From the
  repository root, run `python scripts/render_paper_results.py` to refresh the
  README and HTML tables.

The official pass rule is positive feasibility and normalized quality strictly
greater than `0.4`.
