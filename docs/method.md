# Method

What each reported number means, how it is computed and what it does not show.

## Cohort

The unit is one **authenticated channel announcement**: a BOLT 7 version 1 `channel_announcement` that the observer's LND node accepted, stored and still held when it stopped. An announcement names two node keys and two funding keys and carries one signature under each of the four.

An announcement is included when all of the following hold.

- LND validated it against Bitcoin Core when it arrived: the funding output exists, is unspent and pays to the two-of-two script of the announced funding keys.
- The exporter could reconstruct the complete signed message from the node's graph database.
- All four signatures verify in the Python reader, independently of LND (`wire.py`).
- The announcement sets no channel feature bit. Announcements with feature bits are counted as `unsupported_features` and excluded, and announcements without a stored proof as `missing_proof`. The snapshot had neither.

A bad signature, a duplicate channel identifier or outpoint, or a funding height above the snapshot block is an error that stops the analysis. It is never a silent exclusion.

## Counts

- **Authenticated announcements**: the cohort's size. Each announcement refers to one funding output.
- **Distinct funding keys**: the number of different public keys among both funding keys of all announcements. A key reused across channels counts once.

## Update recency

The cohort is split by whether the node held a `channel_update` of either channel end that is dated within 14 days before the snapshot's observation time. Fourteen days is BOLT 7's pruning horizon, and LND applies it.

- **Recent update observed**: at least one such update. The channel was still advertised.
- **No recent update observed**: none. This does not mean the channel is unused or closed. LND removes channels whose updates are all older than the horizon, so the group mainly holds announcements for which the node held no update at all.

Update timestamps are taken as LND stored them. An update shows that a node advertised routing terms. It does not show that a payment took place.

## Funding-output age

Age is the snapshot block height minus the height of the block that holds the funding output, in blocks. The channel identifier encodes that height. A **nominal year** is 52,560 blocks: 144 blocks a day at Bitcoin's ten-minute target, times 365.

Ages are reported for the announcements with a recent update observed, and only for a group of at least 100. The median and the 90th percentile use the nearest-rank method on exact ages, so each is the age of one observed output. In a group of that size, at least ten outputs are younger and ten are older than either.

The figure and the paper read the distribution at two round ages instead: the share of outputs younger than one nominal year and the share of five nominal years or more. Each share is exact where a published row ends at that age, and is not given otherwise.

Age describes the funding output. It is not the time a key has been public: an announcement can follow its funding by any delay, a key can have been shared or reused before, and splicing can move a continuing channel to a new output.

## Funding value

The value of the cohort is the sum of its funding outputs' values, each output once. A `channel_announcement` carries no amount. LND records the value of the output it validated, and the audit below checks it against the chain.

**Audit against Bitcoin Core** (`funding-value --core-dir`). For every announcement the audit rebuilds the BOLT 3 funding script from the two announced funding keys (sorted, two-of-two), wraps its SHA-256 as a version 0 witness program, and requires Bitcoin Core to report exactly that output script and the recorded value for the funding output. An announcement names its output by a channel identifier: block height, transaction index and output index. The audit reads the output by the transaction identifier that LND resolved from it and requires the block height to match. The output index is compared when the announcement's signatures are verified. The position of the transaction within its block is not compared. It also establishes that the output was unspent at the snapshot block:

- An output that is still unspent when checked was unspent at the snapshot block.
- For an output spent since, the audit finds the spending input in a block after the snapshot block and requires Bitcoin Core's own record of the spent output to show the expected height, script and value.
- An output for which neither holds is reported as not shown and is left out of the audited total.

Every block the audit relies on is tied to the snapshot block through its ancestors, so a chain reorganization during the audit aborts it. The audited total is the value shown unspent at the snapshot block. `verify-value` replays the saved evidence without a node.

The value is what the announced keys guard under one condition: an adversary recovers both private keys of an output while the output is still spendable under them. It is not a loss estimate.

## Readiness

A session becomes a capture only if three observations satisfy one fixed rule (`readiness.py`). They are the node's final view when the session stops and the two periodic observations before the latest periodic one, so that all three lie at least half an hour apart:

- consecutive observations are at least 30 minutes apart,
- each reports a synchronized chain and a synchronized graph and at least three connected peers,
- the set of channels changed by less than 1% between consecutive observations, measured as the symmetric difference divided by the union.

LND reports a synchronized graph once it has requested every channel that one peer listed and received the replies. The rule therefore detects an unfinished or still-growing graph. It is not evidence that the node holds every public channel. We fixed the rule before collecting any mainnet data.

A session stops as soon as its periodic observations satisfy the rule. A session that stops on a budget or on request is judged by the same three observations, and `collection.json` records which of the two happened.

## Closed-channel index

LND keeps an index of channels it considers closed and exports none of them. A failed lookup at the chain backend can put a live channel into that index. After every session the collector checks each entry against Bitcoin Core's set of unspent outputs. An entry whose output is still unspent is a live channel the node excluded. It is disclosed with the results and never repaired. An entry whose lookup fails is counted as unresolved, and a capture with unresolved entries is not published.

## Synchronization coverage

For every historical synchronization of the session, the collector records what the peer listed after LND's own filtering, what the node lacked and what it requested. The first synchronization that requests everything the node lacked marks a complete pass, also when the node lacked nothing. What later peers add shows how much one further peer still contributes.

## Second observer

A second capture, taken by another node identity with its own database, shows whether a group depends on one collection. For each update-recency group of either observer, the comparison counts the announcements that the other one also held. Both observers run the same software on the same network, so agreement is evidence about the collection and not a census. Our second observer was no independent vantage point either: it ran on the same machine and network connection, against the same Bitcoin Core node, about six hours before the snapshot.

## What is not measured

Payment activity, channel balances, unannounced channels, how long a key has been public, and anything about nodes that the observer never heard of.
