# Monthly report fixtures

Drop the real POS monthly report `.TXT` here to run the acceptance test:

```
tests/fixtures/monthly/M-Damansara Jul2026.TXT
```

Any `.TXT`/`.txt` in this directory works — the newest by mtime is used.

Then:

```bash
python -m unittest tests.test_monthly_close_acceptance -v
```

Until a file is present, every acceptance check **skips** (it does not pass).
The skip message names this path. A skipped acceptance run means the monthly
close is unverified against real data.

The July 2026 Damansara file is production data and is deliberately not
committed. The expected figures it is checked against are in
`tests/test_monthly_close_acceptance.py`:

| Figure | Expected |
| --- | --- |
| sales | 148,586.80 |
| COGS | 55,882.30 (37.6%) |
| contribution | 84,889.40 |
| cash_sales | 69,638.50 |
| expected_bank_in | 24,517.40 |
| pinjam addback | 5,378.00 |
| duplicate SKUs | 7 |
| dead SKUs | 131 |

Each is asserted separately, so a failing run names the figure that disagrees
rather than stopping at the first one.
