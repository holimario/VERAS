#!/usr/bin/env bash
set -euo pipefail
cd /work/app
export PYTHONPATH=/work/app
exec python candidate/src/solve.py
