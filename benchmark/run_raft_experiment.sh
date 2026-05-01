#!/bin/bash
# Thin shell wrapper around benchmark/raft_experiment.py.

set -euo pipefail

python3 benchmark/raft_experiment.py both "$@"
