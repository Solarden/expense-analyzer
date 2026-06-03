# XTB regression fixtures

Anonymized `.xlsx` samples of XTB's account export, used by
`tests/importers/test_xtb.py` to guard the parser against the **real file layout**
(zip structure, inline strings, the blank leading column, the full
`Position..Comment` table starting at column B, and the `Name/Account` +
`Balance/Equity` header block).

**No real data.** They are generated, not exported: the account number is
`00000000`, the holder name is blank, amounts are synthetic, and only public
ticker symbols (`SXR8.DE`, `SNT.PL`) appear. The layout — not the values — is what
mirrors a genuine export.

| file | what it covers |
|---|---|
| `sample.xlsx` | happy path: two symbols (one held across two lots), cash + Σ value reconciles to Equity |
| `edge_cases.xlsx` | fractional volume (`0.1980`) and a position at a loss (negative Gross P/L) |
| `broken.xlsx` | a non-numeric `Purchase value` cell → `ImporterError` |

## Regenerating

They are produced by the same in-memory builder the unit tests use, so they never
drift from it:

```bash
PYTHONPATH=src:tests python - <<'PY'
import conftest as cf
cf  # see conftest._build_xtb_xlsx for the exact calls (sample / edge_cases / broken)
PY
```

(see the generation block in the Phase 6 work / `conftest._build_xtb_xlsx`).
