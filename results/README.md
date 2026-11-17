# Published results

Written by `gossip publish`. Do not edit: publish again and replace the folder.

| Quantity | Value |
|---|---|
| Snapshot block | 975,703 |
| Observed at (UTC) | 2026-11-13T19:27:47Z |
| Authenticated channel announcements | 29,814 |
| Distinct funding keys | 59,604 |
| Announcements with a recent update observed | 26,529 |
| Announcements with no recent update observed | 3,285 |
| Funding value shown unspent at the snapshot block (BTC) | 3195.81109755 |
| Median funding-output age (nominal years) | 0.8 |
| 90th percentile of that age (nominal years) | 4.8 |
| Outputs of 5 nominal years or more | 2,471 |

Ages cover the announcements with a recent update observed and are rounded to a tenth of a nominal year (52,560 blocks).

| File | Content |
|---|---|
| `snapshot.json` | Counts, funding keys, update recency, closed-index audit and age quantiles |
| `age-cdf.csv` | Age distribution in twelfths of a nominal year, at least 10 announcements per row |
| `funding-value.json` | Funding value and its audit against Bitcoin Core, when measured |
| `observers.json` | Agreement with a second observer, when compared |
| `collection.json` | Settings, readiness and synchronization coverage of the session, when published |
| `checksums.json` | SHA-256 of the files above |
| `age-cdf.pdf`, `age-cdf.png` | The figure, drawn from `snapshot.json` and `age-cdf.csv` |
| `gossip_numbers.tex` | The reported numbers as TeX macros, for mainnet results with all four JSON files |
