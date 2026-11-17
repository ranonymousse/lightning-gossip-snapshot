#!/usr/bin/env bash
set -euo pipefail
umask 077
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"
command_name="${1:-help}"
if [[ $# -gt 0 ]]; then shift; fi
case "$command_name" in
  sync) exec uv sync --locked "$@" ;;
  test)
    uv run --locked ruff check src tests scripts
    uv run --locked ruff format --check src tests scripts
    exec uv run --locked pytest "$@"
    ;;
  build) exec uv run --locked python scripts/build_exporter.py "$@" ;;
  collect) exec uv run --locked gossip collect "$@" ;;
  core) exec uv run --locked gossip core "$@" ;;
  export)
    if [[ ! -x bin/gossip-export ]]; then
      echo 'Build the offline exporter first: ./run.sh build' >&2
      exit 2
    fi
    exec bin/gossip-export "$@"
    ;;
  reproduce)
    # Regenerate the figure and the macro file from the published results alone.
    run_dir="$project_dir/output/reproduce-$(date -u +%Y%m%dT%H%M%SZ)"
    uv run --locked gossip figures --results results --output "$run_dir/figure" > /dev/null
    uv run --locked gossip paper-numbers --results results --output "$run_dir/gossip_numbers.tex" \
      > /dev/null
    if cmp -s "$run_dir/gossip_numbers.tex" results/gossip_numbers.tex; then
      echo 'Macro file: identical to results/gossip_numbers.tex'
    else
      echo 'Macro file: DIFFERS from results/gossip_numbers.tex' >&2
      exit 1
    fi
    printf 'Figure redrawn from results/age-cdf.csv: %s/figure/age-cdf.pdf\n' "$run_dir"
    ;;
  demo)
    # The whole pipeline on signed synthetic announcements. No node, no network.
    run_dir="$project_dir/output/demo-$(date -u +%Y%m%dT%H%M%SZ)"
    uv run --locked gossip fixture --records 300 --output "$run_dir/capture"
    uv run --locked gossip analyze --capture "$run_dir/capture" --output "$run_dir/analysis" \
      > /dev/null
    uv run --locked gossip verify --capture "$run_dir/capture" --output "$run_dir/analysis"
    uv run --locked gossip publish --capture "$run_dir/capture" --output "$run_dir/results"
    printf 'Synthetic results, labelled as such: %s/results\n' "$run_dir"
    ;;
  fixture|analyze|verify|publish|figures|paper-numbers|funding-value|verify-value|compare-observers|verify-observers|readiness)
    exec uv run --locked gossip "$command_name" "$@"
    ;;
  help|--help|-h)
    cat <<'HELP'
Without a node or network access:
  ./run.sh sync                         Install the locked Python environment
  ./run.sh test                         Lint and run the tests
  ./run.sh reproduce                    Regenerate the figure and the macro file from results/
  ./run.sh demo                         Run the whole pipeline on synthetic announcements
  ./run.sh analyze --capture DIRECTORY --output NEW_DIRECTORY
  ./run.sh verify --capture DIRECTORY --output DIRECTORY
  ./run.sh publish --capture DIRECTORY --output NEW_DIRECTORY [--value DIRECTORY --sample FILE]
                   [--observers DIRECTORY --other DIRECTORY] [--session-result FILE]
                                        Write the publishable aggregates of a capture
  ./run.sh figures --results DIRECTORY --output NEW_DIRECTORY
  ./run.sh paper-numbers --results DIRECTORY --output NEW_FILE
  ./run.sh funding-value --capture DIRECTORY --sample FILE --output NEW_DIRECTORY
  ./run.sh verify-value --capture DIRECTORY --sample FILE --output DIRECTORY
  ./run.sh compare-observers --capture DIRECTORY --other DIRECTORY --output NEW_DIRECTORY
  ./run.sh verify-observers --capture DIRECTORY --other DIRECTORY --output DIRECTORY
  ./run.sh readiness SAMPLES.json

Building the exporter (downloads the pinned LND sources and Go modules):
  ./run.sh build [--source PATH] [--with-node]
  ./run.sh export --db COPY --output NEW_DIRECTORY

Bitcoin Core chain backend (Linux, downloads the full chain):
  ./run.sh core install                Download the pinned release and check its SHA-256
  ./run.sh core start                  Start the node in the background
  ./run.sh core check-lnd              Probe pinned LND against the node before the long sync
  ./run.sh core status --follow        Watch synchronization
  ./run.sh core stop                   Clean shutdown over RPC

Collection (Linux, real outbound connections to the Lightning network):
  ./run.sh collect start --hours 8     Prepare, unlock the wallet, then show the dashboard
  ./run.sh collect watch               Reattach the dashboard (Ctrl+C detaches)
  ./run.sh collect status              Show progress and result paths
  ./run.sh collect stop                Request a clean shutdown
  ./run.sh collect audit-closed --session ID
                                       Recheck closed channels against Bitcoin Core

See README.md and docs/ for what each step does and what it needs.
HELP
    ;;
  *) echo "Unknown command: $command_name (see ./run.sh help)" >&2; exit 2 ;;
esac
