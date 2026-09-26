# Reproducibility Notes

## Harness verification

From the repository root:

```bash
./scripts/verify_artifact.sh
```

The VERAS harness expects a compatible ORAgentBench checkout and a
user-provided OpenAI-compatible endpoint, model identifier, and credential as
documented in `veras/README.md`. No endpoint or credential is stored in this
artifact.

Without that external checkout, `scripts/verify_artifact.sh` still runs the
anonymization scan and the 14 dependency-free semantic-observer tests. With the
checkout present, it also runs the full 32-test harness suite.

## Real-world algorithms

The generated source is provided for inspection and authorized replay. The
data-center algorithms expect their six Parquet inputs under `/work/app/data`.
The power-grid algorithms expect the neutral model inputs under `/work/app` and
require a compatible MOSEK installation and license.

The private operational inputs cannot be distributed with an anonymous public
artifact. Authorized users must place those inputs at the paths documented by
the corresponding case before replay.
