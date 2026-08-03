# Synthetic monthly report fixture

**This is NOT the real July report.** It is a hand-built file that reproduces the
*structure* of the real monthly report — the literal closing-total lines, the
twice-printed `DESCRIPTION AMOUNT` header, inline `_PAIKKASU_` tags on both
`PAY TO` and `NON PAY TO` rows, `ADVANCE TO` rows inside the payouts block, and
the summary labels.

Its row amounts are chosen so that every block reconciles to its own printed
closing total, and so the close lands on the known-good figures. That makes it a
valid test of the *parsing and classification logic* — it cannot validate that
the real file looks like this.

The real July 2026 Damansara file is production data and belongs in
`tests/fixtures/monthly/` (see that directory's README). Only the acceptance
test against that file can confirm the structure is right.
