# Collecting a snapshot

This runbook takes a Linux machine from an empty disk to a published results folder. Everything a session writes is private. Only the last step produces files meant to be shared.

## What you need

- Linux on x86_64 or aarch64.
- About 1 TB of disk. The node keeps the full, unpruned chain (about 892 GB in November 2026), and `core install` refuses to start with less than 900 GiB free.
- [uv](https://docs.astral.sh/uv/), Git, a Go toolchain of version 1.21 or later and C build tools. Go 1.21 can download the pinned release toolchain by itself.
- Outbound network access. The machine accepts no inbound connection.

We did not determine minimum memory or CPU requirements.

## 1. Build

```bash
./run.sh sync
./run.sh test
./run.sh build --with-node
```

`build` clones LND at the revision pinned in `versions.json` into `.cache/lnd` and refuses any other revision. It adds the exporter in `cmd/gossip-export/`, runs its round-trip test on a synthetic graph database, checks the exported bytes with the Python verifier, and compiles `gossip-export`, `lnd` and `lncli` into `bin/`. The node binaries are built from unmodified sources. `bin/build-manifest.json` records the exporter's source hashes, the binary hashes and the Go version.

Two limits of these checks. The build compares the checkout's tracked files with the pinned revision and does not look for untracked files, so let it make its own clone or pass a clean one with `--source`. It uses the Go toolchain pinned in `versions.json` unless the environment already sets `GOTOOLCHAIN`, and the manifest shows which version ran.

## 2. Bitcoin Core

```bash
./run.sh core install          # pinned release, SHA-256 checked
./run.sh core start
./run.sh core check-lnd        # before the long download: does LND accept this node?
./run.sh core status --follow  # until synchronized
```

The node lives in `~/.local/state/gossip-snapshot/bitcoin-core/` (`./run.sh core --dir PATH …` chooses another place). Its generated configuration keeps the full chain, has no wallet, accepts no inbound peers and serves RPC on the loopback interface only.

`core check-lnd` starts the pinned LND in a throwaway directory, waits for its handshake with the node, stops it and deletes the directory. Run it while Bitcoin Core is still synchronizing: LND then waits for the chain and contacts no Lightning peer. Against a synchronized node, LND may begin to look for peers in the moment before the probe stops it.

Initial synchronization takes days on ordinary hardware. `core start --dbcache-gb N` (1 to 64) gives the node a larger database cache for that run. `core stop` asks the node to shut down over RPC and waits while it writes its cache. Do not kill the node: an unclean stop repeats validation.

**Why unpruned.** LND checks every announced channel by reading its funding block and its unspent output from this node. A pruned node would have to fetch old blocks from Bitcoin peers again, and a failed lookup makes LND record a live channel as closed.

## 3. Collect

```bash
./run.sh collect start --hours 8 --max-download-gb 50 --max-data-gb 60
```

`start` prepares the observer directory (`~/.local/state/gossip-snapshot/observer`), refuses to continue unless Bitcoin Core is synchronized, on mainnet and unpruned, starts LND in the background and asks you to create the node's wallet. Use a new, empty wallet, keep its seed private and never fund it. The dashboard then shows progress. Ctrl+C leaves the dashboard and keeps the session running.

```bash
./run.sh collect watch          # reattach the dashboard
./run.sh collect status --json  # the saved status as JSON
./run.sh collect stop           # request a clean shutdown
```

Further commands: `collect init` prepares the observer directory without starting a session, `collect wallet` repeats the wallet prompt, `collect run` runs a session in the foreground with the same options, and `collect audit-closed --session ID` repeats the closed-index audit into a new file. To use another observer directory, put `--dir` before the sub-command, as in `./run.sh collect --dir PATH start …`.

A session ends by itself when it reaches readiness. It also ends when a budget is spent or when synchronization stalls:

| Option | Default | Meaning |
|---|---|---|
| `--hours` | 4 | Time ceiling of the session |
| `--max-download-gb` | 10 | Ceiling on what the whole machine receives, Bitcoin Core included |
| `--max-data-gb` | 30 | Ceiling on the observer directory's size |
| `--min-free-gb` | 10 | Free disk below which the session stops |
| `--target-peers` | 6 | Lightning peers the collector keeps connected (3 to 12) |
| `--stall-minutes` | 45 | Stop after this long without graph progress while unsynchronized |
| `--historical-sync-minutes` | 60 | Interval of LND's reconciliation with a further peer (LND's default) |
| `--block-cache-gb` | 2 | LND's block cache |
| `--keep-slow-peers` / `--disconnect-slow-peers` | keep | Whether LND keeps a peer whose pong arrives late |
| `--debug-sync` | off | Debug-level logs of LND's gossip, graph and peer subsystems |
| `--workers` | 0 | Processes for signature verification after the session (0 chooses) |
| `--core-dir` | the default node | The Bitcoin Core directory, read when the observer directory is created |

**The session behind the published results** ran in a new observer directory with

```bash
./run.sh collect start --hours 12 --max-download-gb 50 --max-data-gb 60 --debug-sync
```

and ended at readiness after 2 hours and 6 minutes. `results/collection.json` records the four budgets, the debug log and the stop at readiness. The command line, the duration and the dial counts below come from the session's private record.

**How LND is started.** The node configuration is `config/lnd.conf.example` with the path of Bitcoin Core's cookie filled in. The collector adds options on the command line. For the session above, the full line was

```text
bin/lnd --lnddir=OBSERVER/lnd --configfile=OBSERVER/lnd/lnd.conf --blockcachesize=2147483648
        --no-disconnect-on-pong-failure --debuglevel=info,DISC=debug,GRPH=debug,PEER=debug
```

- `--blockcachesize` is always passed (`--block-cache-gb`).
- `--no-disconnect-on-pong-failure` is passed unless you choose `--disconnect-slow-peers`. LND reads a peer's pongs in the same loop as its gossip, so the pong of a peer that is sending many channels can be read too late. In our earlier sessions, LND's default dropped the very peer that was serving the synchronization.
- `--debuglevel` is passed with `--debug-sync`, and `--historicalsyncinterval` when `--historical-sync-minutes` differs from 60.

In the configuration, `routing.assumechanvalid=false` keeps channel validation on, `nolisten=true` accepts no inbound peer, `bitcoind.rpcpolling=true` learns of new blocks by polling Bitcoin Core's RPC interface, and `numgraphsyncpeers=3` sets how many peers send the node new gossip. `gossip.ban-threshold=0` turns off LND's banning of gossip peers altogether. We set it because LND raises a peer's ban score for every channel it announces that sits in the node's own closed-channel index, which can disconnect every peer the node synchronizes from. LND still rejects each invalid message. It only stops disconnecting the sender.

**What the node and the collector do on the network.** LND finds its first peers by itself, from DNS seeds and from its graph. The collector then keeps the peer count at its target: it asks LND, through the node's local admin interface, to connect to public nodes whose addresses the node learned from gossip. It dials literal public IP addresses only, never a host name or an onion address, at most two at a time, and backs off for ten minutes or longer after a failed dial. While the graph is still synchronizing, it replaces at most one peer every five minutes, and only for one of three reasons: the peer's range replies were rejected repeatedly, it left a synchronization query unanswered for ten minutes, or the graph made no progress for twenty minutes. The admin interface is used for connecting and disconnecting peers and for nothing else. The node opens no channel and sends no payment. In the session above the collector made 60 dials, of which 12 connected, and replaced no peer.

### After the session

`collect status --json` reports the outcome in `state`:

- **`verified`**: the readiness rule held, the node stopped cleanly, its final view equals the export, all signatures verify and the closed-channel index was audited. The session directory under `sessions/` now holds `capture/` and `analysis/`, the exact private analysis.
- **`diagnostic`**: the readiness rule did not hold. The export has no manifest, the analysis refuses it, and the reasons are listed under `readiness`. Start again in the same directory to continue from the synchronized database.
- **`incomplete`**: the node stopped cleanly, but a check after the shutdown failed, for example because the graph changed while the node stopped. The reason is under `error`, and the database is kept.
- **`failed`**: the session or the shutdown itself failed. Nothing was exported.

Check four values in the status. `stop_reason` says why the session ended. `closed_audit.unspent_at_audit` must be 0: anything else is a live channel that the node excluded. `closed_audit.unresolved` must be 0 as well, or `publish` refuses the capture. `newest_funding_lag_blocks` should be small: a lag of thousands of blocks marks a synchronization that stopped early. `sync_coverage` lists, for each historical synchronization, what the peer listed, what the node lacked and what it requested. After the first complete synchronization, later peers should add little.

Bitcoin Core must keep running until the session has ended, because the closed-index audit needs it. If the node was down during a session, start over in a new observer directory.

## 4. Funding value

On the collection machine, with Bitcoin Core still running:

```bash
S=~/.local/state/gossip-snapshot/observer/sessions/SESSION_ID
./run.sh funding-value --capture $S/capture --sample $S/final-sample.json \
  --output $S/funding-value --core-dir ~/.local/state/gossip-snapshot/bitcoin-core
./run.sh verify-value --capture $S/capture --sample $S/final-sample.json --output $S/funding-value
```

`funding-value` sums the value of each funding output once. With `--core-dir` it also rebuilds every funding script from the two announced keys and checks script, value and unspentness at the snapshot block against Bitcoin Core. `verify-value` replays the saved evidence without a node. [`method.md`](method.md) describes the audit.

## 5. Publish

```bash
./run.sh publish --capture $S/capture --output NEW_RESULTS_FOLDER \
  --value $S/funding-value --sample $S/final-sample.json \
  --session-result $S/result.json
```

`publish` analyzes the capture again, replays the value artifact and writes aggregates only, as [`data.md`](data.md) describes. It refuses a capture whose closed-index audit left entries unresolved, and it refuses to write a folder in which the scan finds anything shaped like a key, a script, a channel identifier, an outpoint, an address or a path. Add `--observers` and `--other` to include a comparison with a second observer's capture (`./run.sh compare-observers`).

With all of these inputs, `publish` also writes `gossip_numbers.tex`, the numbers a paper would report. That needs more than the data files do: an audited value with every output shown unspent, ages, and one synchronization that requested everything the node lacked. Where one of them is missing, the folder is written without the macro file and its `README.md` says why.

Copy a session to another machine with ordinary tools if you want to analyze it elsewhere, and treat the copy as private.
