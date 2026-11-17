# Data reference

What a published results folder holds, what stays private, and how to check the folder.

## Published: `results/`

`gossip publish` writes these files. `checksums.json` lists the SHA-256 of the five data files, and `figures`, `paper-numbers` and `./run.sh reproduce` refuse a folder in which a file no longer matches. `tests/test_results.py` checks the committed folder: its checksums, the scan, the row rule, its sums and the generated files.

Every JSON data file carries `schema_version` (1) and `dataset_kind` (`mainnet` or `synthetic`). Fields named `note` or `interpretation` hold a fixed sentence on how to read the numbers beside them. The files call the snapshot block `reference`. Some counts of announcements are named `channels`: each announcement describes one channel. Times are in UTC, to the second.

| File | Content |
|---|---|
| `snapshot.json` | Counts, funding keys, update recency, closed-index audit, age quantiles |
| `age-cdf.csv` | Age distribution of the announcements with a recent update observed |
| `funding-value.json` | Funding value and its audit against Bitcoin Core |
| `observers.json` | Agreement with a second observer |
| `collection.json` | Settings, readiness and synchronization coverage of the session |
| `checksums.json` | SHA-256 of the files above |
| `README.md` | Headline numbers, generated |
| `age-cdf.pdf`, `age-cdf.png` | The figure, drawn from `snapshot.json` and `age-cdf.csv` |
| `gossip_numbers.tex` | The numbers the paper reports, as TeX macros with their input hashes |

The last three data files are optional. A folder without them has no macro file, and a folder with them has one only where the reported numbers follow from it (see below).

### `snapshot.json`

| Field | Meaning |
|---|---|
| `captured_at`, `reference` | Observation time (UTC) and the block the snapshot refers to |
| `backend`, `lnd_commit`, `cohort` | Chain backend, the LND revision of node and exporter, the cohort's definition |
| `exported_records`, `included_records`, `excluded_records`, `exclusions` | What the exporter wrote and what the cohort kept, with reasons for exclusion |
| `counts.authenticated_announcements`, `counts.distinct_funding_keys` | The headline counts |
| `counts.update_recency` | Both counts for each group, the window in seconds with the time it ends at, and the number of announcements without any stored update |
| `closed_index` | Entries in the node's closed-channel index and the outcome of their audit |
| `funding_output_age.median_tenths`, `.p90_tenths` | Median and 90th percentile of funding-output age, in tenths of a nominal year, rounded to nearest with ties to even |
| `funding_output_age.outputs_at_least_5_years` | Outputs aged five nominal years or more, or `null` where no row of `age-cdf.csv` starts at five years |
| `funding_output_age.group`, `.unit`, `.quantile_method` | The group the ages cover, their unit and how the quantiles are taken |
| `age_cdf` | Step and minimum row size of `age-cdf.csv` |
| `inputs` | SHA-256 of the private capture's manifest, export report and records |

Ages cover the announcements with a recent update observed. `funding_output_age` is `null` and `age-cdf.csv` has no rows when fewer than 100 such announcements exist.

### `age-cdf.csv`

One row per age range. A row counts the announcements whose funding output is at least `age_from_blocks` and less than `age_below_blocks` old, with the running total and its share. Ranges are twelfths of a nominal year (4,380 blocks), merged towards older ages until each row holds at least ten announcements. The last row ends at a whole nominal year, so the file does not state the oldest output's age.

### `funding-value.json`

| Field | Meaning |
|---|---|
| `included_outputs`, `funding_value_satoshis`, `funding_value_btc` | Outputs and their summed value as the node recorded them |
| `by_update_recency` | The same for each update-recency group |
| `value_source` | Where the values come from: the node's record of each validated output |
| `core_audit.outputs_checked`, `.scope` | Outputs checked against Bitcoin Core, and what the check covers |
| `core_audit.outputs_by_state` | Outputs per state: unspent when checked, spent in a block after the snapshot block, not shown |
| `core_audit.confirmed_unspent_at_reference` | The audited total: outputs and value shown unspent at the snapshot block |
| `core_audit.started_at`, `.finished_at`, `.tip_start`, `.tip_end`, `.blocks_scanned_after_reference` | When the audit ran, the chain tips at its start and end, and the blocks it searched for spends |
| `inputs` | SHA-256 of the capture files and of the final node view the values were read from |

A value is `null` where it would describe fewer than ten outputs, or where two published values could differ by the value of fewer than ten. The split by update recency is therefore published only when the audit showed every output unspent, or when no audit ran. When fewer than ten outputs were not shown, only the audited total is published. The audit's states are counts without values. `core_audit` is `null` when the value was not audited.

### `observers.json`

`observer_in_second` gives, for each update-recency group of the snapshot, its announcements (`channels`), how many the second observer also held (`held`) and how many it did not (`not_held`). `second_in_observer` gives the same for the second observer's groups, located in the snapshot. `observer` and `second_observer` give each capture's time, block and input hashes.

### `collection.json`

| Field | Meaning |
|---|---|
| `session.stopped_at_readiness` | Whether the session ended because the readiness rule held, and not on a budget or a request |
| `session.limits` | The session's budgets: its time ceiling in hours, and received data, observer directory size and minimum free disk in GiB |
| `session.debug_log` | Whether LND wrote debug-level logs of its gossip, graph and peer subsystems |
| `readiness.churn` | Share of the channel set that changed between the first and second and between the second and third observation |
| `readiness.ready`, `.rule` | That the rule held, and its thresholds |
| `sync_coverage` | One entry per historical synchronization, in order: what the peer listed (`advertised`), what the node lacked (`new`) and what it requested (`queried`). `null` marks a synchronization without a channel list |
| `inputs` | SHA-256 of the capture's manifest and of the session's private result file |

### `gossip_numbers.tex`

One `\newcommand` per number that the paper reports, preceded by comments with the hashes of the four JSON files it was generated from. The paper inputs this file and types none of these numbers by hand. The file is written for a mainnet folder with all four JSON files in which these numbers follow from the data: an audited value with every output shown unspent, ages, and one synchronization that requested everything the node lacked. Otherwise the folder's `README.md` names what is missing.

## Publication rules

1. **At least ten per row.** No row of the age distribution describes fewer than ten announcements.
2. **Ages for a hundred or more, and no extremes.** Ages are published for a group of at least 100 announcements. Beside the distribution they appear as two quantiles in tenths of a nominal year. Each quantile is the age of one observed output, and in a group of that size at least ten outputs are younger and ten are older. The number of outputs of five years or more is published where a row starts at five years, so that it follows from the rows. The oldest and youngest ages are not published. The figure's two labels are shares counted from the rows, in whole percent and each only where a row ends at its age: the announcements younger than one nominal year and those of five nominal years or more.
3. **At least ten per value.** A funding value is published for a set of at least ten outputs, and no two published values differ by the value of fewer.
4. **Fields by list, then a scan.** Each file is built field by field in `publish.py`. Paths, the collector's revision, the machine's resources, peer identities and the times of single network events are never copied. The published times are those of the observation, of the update window and of the two audits. As a second line of defence, `publish` then refuses to write a folder that holds a file it does not know, a string shaped like a public key, an outpoint, a channel identifier in decimal, grouped, hexadecimal or either block notation, a session identifier, a Bitcoin address, an IPv4, IPv6 or onion address, an e-mail address or a path, a run of 40 or more hexadecimal digits outside the fields named as hashes, or a distribution row below ten. It reads the files as text and every JSON string once more as decoded. `tests/test_publish.py` exercises each case. The scan knows the forms that a capture and a session hold. It cannot recognise a host or user name, a node's alias, or a value in another encoding or spread over several fields, which is why the field lists come first.

Plain counts are published as they are, including small ones: the entries of the closed-channel index, what one synchronization added, what the second observer did not hold, or how many channels changed between two observations. A plain count carries no age or value of a channel.

The published hashes cover private files whose content cannot be guessed: the signed records, the node's graph, the session's record. They let us show later that a published number belongs to a given capture. No hash of the private value summary is published, because that file holds little beyond the published numbers and a withheld value could be found by trying.

## Private: a session and its capture

A session directory holds, among others:

| Item | Content |
|---|---|
| `capture/records.jsonl` | Every exported announcement with its keys, channel identifier, outpoint and signed bytes |
| `capture/closed-scids.txt`, `closed-audit.json` | The node's closed-channel index and its audit |
| `capture/readiness.json` | The observations behind the readiness decision, each with the node's channel list |
| `capture/manifest.json`, `export-report.json` | Hashes, counts, snapshot block and the attestations of the collection |
| `final-sample.json`, `observations/` | The node's graph as it reported it, with node addresses and peers |
| `lnd.log`, `peer-events.jsonl` | Logs with peer keys and addresses |
| `channel.db` | A copy of the node's graph database |
| `analysis/` | The exact analysis: `summary.json` and an age table in 1,000-block bins |
| `funding-value/` | The value summary, and `core-evidence.jsonl` with one line per output: outpoint, script and value |

None of these is published. `src/gossip_snapshot/schema.py` is the exact contract of the capture files. To see a capture without collecting one, run `./run.sh fixture --output NEW_DIRECTORY`, which writes a signed synthetic one.
