# Lightning gossip snapshot

Replication package for the measurement in *SoK: What Still Holds? Bitcoin's Security and Privacy Another Decade Later* (under review, anonymized).

A Lightning channel's funding output shows only a script hash on the Bitcoin blockchain. The channel's public announcement, however, names both funding keys behind that hash. This package measures how many such keys an ordinary Lightning node receives through public gossip and how much bitcoin they guard. It contains the collection and analysis code, signed synthetic test data and the aggregate results of our snapshot.

**What it does not contain.** No captured key, channel identifier, funding outpoint, node identifier, peer address, log or database. They stay on the machine that collected them. [Data handling](#ethics-and-data-handling) says how the published results are kept free of them.

## Results

[`results/`](results/) holds the published aggregates of the snapshot of 13 November 2026, and [`results/README.md`](results/README.md) lists the headline numbers. The paper's figure (`results/age-cdf.pdf`) and its number macros (`results/gossip_numbers.tex`) are generated from those files and from nothing else. [`docs/data.md`](docs/data.md) describes every file and field.

## Three ways to use this package

The package regenerates the paper's figure and numbers from the published aggregates, runs the whole pipeline on synthetic data, and lets you collect a snapshot of your own. It cannot recompute our numbers from our capture, because the capture stays private.

You need [uv](https://docs.astral.sh/uv/), which installs Python 3.12 and the locked dependencies. Linux and macOS are supported. Run the commands from a checkout of this repository.

**1. Regenerate the paper's figure and numbers** from the published aggregates. It takes about a minute and needs no network after the first command.

```bash
./run.sh sync
./run.sh reproduce
```

`reproduce` redraws the figure and rewrites the macro file from `results/`, and fails unless the macro file is byte-identical to the published one. A redrawn PDF can differ in its compressed bytes between platforms, so the figure is not compared.

**2. Run the whole pipeline on synthetic data.** No node and no network.

```bash
./run.sh demo    # 300 signed synthetic announcements: verify, analyze, publish, draw
./run.sh test    # lint and tests
```

**3. Collect your own snapshot.** This needs a Linux machine with about 1 TB of disk, a full Bitcoin Core synchronization and a collection session of a few hours. [`docs/collecting.md`](docs/collecting.md) is the runbook.

| Command | Network access |
|---|---|
| `sync` | Python packages, once |
| `test`, `demo`, `reproduce`, `analyze`, `verify`, `publish`, `figures`, `paper-numbers` | none |
| `build` | LND sources at the pinned revision, Go modules, possibly the pinned Go toolchain |
| `core install`, `core start` | the pinned Bitcoin Core release, then the Bitcoin network |
| `collect` | outbound connections to public Lightning nodes |
| `funding-value --core-dir` | the local Bitcoin Core only |

## How it works

```text
Bitcoin Core, unpruned and local                     core.py
└─ LND, pinned and unmodified, empty wallet          collector.py, peers.py
   one bounded session, ended at readiness           readiness.py
   └─ stopped graph database
      └─ read-only exporter                          cmd/gossip-export (Go)
         └─ capture: signed announcements, closed-channel index and its audit
            ├─ four signatures per announcement      wire.py, verification.py
            ├─ counts, funding keys, update recency, exact ages      analysis.py
            ├─ funding value, audited against Bitcoin Core           funding_value.py
            ├─ agreement with a second observer                      observers.py
            └─ publishable aggregates                publish.py
               ├─ the figure                         figures.py
               └─ the number macros                  paper_numbers.py
```

The observer is an LND node, built from unmodified sources, that opens no channel and sends no payment. From its first start it validates every announced channel against our own Bitcoin Core node. After the session, the exporter reads the stopped node's graph database and reconstructs each signed `channel_announcement` with its two funding keys. Python then verifies all four signatures of every announcement independently of LND. The value step rebuilds each two-of-two funding script from the announced keys and compares script, value and unspentness with Bitcoin Core, output by output. [`docs/method.md`](docs/method.md) defines every reported number.

## What makes a capture valid

A session yields a capture only if all of the following hold. Otherwise it yields a diagnostic export that the analysis refuses.

- The observer directory was created by `collect init` with channel validation enabled, and its configuration and binaries are checked against their recorded hashes at every start.
- The session reached **readiness**. The rule looks at three observations at least 30 minutes apart: the node's final view when the session stops and two earlier periodic ones. Each must report a synchronized chain and graph and at least three peers, and the channel set must have changed by less than 1% between consecutive observations.
- The node shut down cleanly, and its final view equals the exported graph at the same block.
- Every announcement in the cohort carries four valid signatures.
- The node's closed-channel index was audited against Bitcoin Core's set of unspent outputs. An entry whose lookup failed is recorded as unresolved, and `publish` refuses a capture with unresolved entries.

The readiness rule was fixed before we collected any mainnet data and was not tuned afterwards. It detects an unfinished or still-growing synchronization. It does not establish that the node saw the whole network. A session normally stops as soon as its periodic observations satisfy the rule, and `collection.json` records whether it did.

The collector does not record whether a session is the first of its observer directory. A later session resumes the directory's graph database and can yield a capture too. That the reported capture started from an empty database is our statement ([Provenance](#provenance)).

## Scope and limits

- **One observer on one day.** The results are what our node received. They are not a census of the Lightning network, and channels that were never announced do not appear in gossip at all.
- **Lower bounds for what was observed.** Every counted funding output was verified against the blockchain, so announcements the node did not receive could only raise the counts and the value.
- **Value is conditional.** The funding value is what the announced keys guard if an adversary recovers both private keys of an output. It is not an expected loss.
- **Age describes the funding output.** It is not how long a key has been public: a channel can be announced after it was funded, and splicing can move a continuing channel to a new output.
- **Recent update observed** means the node held a channel update dated within 14 days before the snapshot. It shows that a channel was still advertised, not that it carried payments.
- **The group without a recent update varies.** Across our three verified sessions it held between about one thousand and about ten thousand announcements, depending on what single peers happened to send, while the group with a recent update stayed within one percent. Expect a different total from a snapshot of your own. The value moves little with it: that group held about one to two percent of the value each time.

## Ethics and data handling

The observer takes part in the network as any node does. It runs LND built from unmodified sources at the pinned revision, with the configuration and the command-line options that [`docs/collecting.md`](docs/collecting.md) lists. It finds its first peers through LND's own bootstrap, connects to public peers (six by default), opens no channel, sends no payment and probes nothing. Channel announcements are public messages that every participating node receives and stores. The exporter adds nothing to what the node already holds: it reads the funding keys from the node's own database, which the node's query interface does not return.

What a session writes stays private: keys, channel identifiers, outpoints, peer addresses, logs and the database. Only `gossip publish` produces files meant to leave the machine, under four rules.

- The age distribution is published in twelfths of a nominal year, and rows are merged until each describes at least ten announcements. The oldest row ends at a whole nominal year.
- Ages are published for a group of at least 100 announcements. Apart from the distribution they appear as two quantiles in tenths of a nominal year, each at least ten announcements away from the youngest and the oldest output, and as the number of outputs of five years or more where that number follows from the rows. No extreme value is published.
- A funding value is published for a set of at least ten outputs, and no two published values differ by the value of fewer.
- Every published file is built field by field from explicit lists. As a second line of defence, the folder is then scanned for anything shaped like a key, a script, a channel identifier, an outpoint, an address or a path, and for any file that does not belong there. A hit stops the publication. The scan knows the forms that a capture holds. It cannot recognise a host or user name, a node's alias, or a value in another encoding or spread over several fields.

Plain counts are published as they are, small ones included: the entries of the closed-channel index, or the channels that one synchronization added. They carry no age or value of a channel. [`docs/data.md`](docs/data.md) lists every published field.

We publish aggregates and no channel-by-channel list. The announcements themselves are not secret: every Lightning node holds them, and the exporter and the value step of this package derive such a list from one's own node, as they did from ours. Our choice is not to hand out a ready-made list of keys with the funds they guard. The package contains no tool that targets a channel or a node.

The observer's peers saw its network address, as they see any node's. The published times and synchronization sizes would let such a peer recognise the observer in its own logs.

## Provenance

- **Sessions.** We ran six mainnet sessions between 10 and 13 November 2026. The first five were development runs, and the sixth is the measurement. The first three used a light-client chain backend. They led us to add peer management to the collector and then to replace that backend with Bitcoin Core, because its failed block lookups made LND record live channels as closed. The fourth session, the first on Bitcoin Core, ended without a completed synchronization and led to the stall detection and the time limits that the collector has now. The fifth was the first to pass every check, but it had resumed the database that the fourth left behind. It is the second observer in `observers.json`: another LND identity with its own database, on the same machine and Bitcoin Core node as the sixth, about six hours earlier. Before the sixth, we decided to report the first capture that started from an empty database and passed every check, whatever its counts. The sixth session did so on its only attempt and is the reported capture. After the cleanup described below, a seventh session on 17 November 2026 served as a functional test of this code, again from an empty database. It passed every check, and its results are not reported.
- **What stayed fixed.** The readiness rule has stood since the first version of the code, before any mainnet data. The fixes between sessions concerned how the collector supervises the node. The export gained update timestamps and the closed-channel index along the way.
- **The capture predates this release.** We collected it with an earlier revision of this code and cleaned the code afterwards: tooling for transferring and inspecting sessions was removed, commands and modules were renamed, and the publication step was added. No check, threshold or formula of the measurement changed.
- **The released code reproduces the published numbers from that capture.** On the original capture, `verify-value` and `verify-observers` accept the stored value audit and observer comparison unchanged, and `publish` writes the files in `results/`.
- **Unchanged since collection.** The exporter (`cmd/gossip-export/`), the signature verification (`wire.py`, `verification.py`) and `uv.lock` are byte-identical to the files whose hashes the reported session recorded. `tests/test_provenance.py` holds those hashes and fails if a file differs. The build script, the node configuration template and `versions.json` have not changed either. Comments in these files are left as they were, and a few name files from before the cleanup.
- **The word pilot.** Until this release the collector called every mainnet capture a pilot and wrote `"pilot": true` into its manifest. It had no other mode: which capture to report was our decision, taken by the rule above and outside the collector. The reported capture and the second observer's capture both carry the field. The format still accepts it, because the stored value audit covers the manifest's hash. The collector no longer writes it and nothing reads it.
- **Format numbers.** Private files carry format numbers above 1 (export 2, value summary 2, analysis summary 3). They count revisions made before this release.
- **What a reader can check.** The hashes in `results/` commit to the private capture, its value audit and the session's record, so the capture can be checked against the published numbers without being published. An outside reader cannot recompute the numbers. The statements in this section about the capture and its sessions rest on our word.
- **Pinned software:** LND `v0.21.4-beta` and Bitcoin Core `29.5`, by commit and SHA-256 in `versions.json`. The Bitcoin Core release signatures are not checked by this code.
- **AI assistance.** As the paper discloses, an AI assistant helped develop this code. The authors reviewed and verified it.

## Layout

```text
results/                  Published aggregates, figure and number macros
src/gossip_snapshot/      Python package behind the `gossip` command
cmd/gossip-export/        Read-only Go exporter for the LND graph database
scripts/build_exporter.py Pinned build of the exporter and, optionally, the node
config/lnd.conf.example   Node configuration used for collection
tests/                    Tests without network access, on synthetic data and on results/
docs/                     Runbook, method and data reference
versions.json, uv.lock    Pinned software and dependencies
```

## License and citation

MIT, see [`LICENSE`](LICENSE). Please cite the paper. Its reference will be added here after review.
