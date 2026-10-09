# Clipper Fund: Phase 1 Data Exploration

## Problem definition

The fund manager must choose an initial cash buffer and, after investor redemptions occur, which assets to sell. The proposed two-stage model balances the assumed equity-over-cash opportunity cost with scenario-weighted execution costs. Sales must respect holdings and sale/settlement capacity, and cash plus proceeds net of costs must cover modeled redemptions. [Clipper's prospectus](https://clipperfund.com/documents/CFProsp.pdf) describes cash, security sales and other redemption arrangements. The current files support a portfolio snapshot, not an empirically validated optimal-buffer recommendation.

## Observed local coverage

- Fund: Clipper Fund (S000011372); class CFIMX.
- Distinct filings: 1; report dates: 1.
- Latest report date: 2026-06-30; filed: 2026-08-27.
- Monthly observations: 3 (2026-04 to 2026-06).
- Holdings in latest filing: 35; common equities: 32.
- NAV: USD 1,476,125,004.95; B.2.f residual cash: USD 198.19.
- Cash plus cash-like asset proxy: USD 95,476,198.19 (6.468029% of NAV), conditional on availability assumptions.
- Other-assets residual: USD 2,698,021.25; requires a financial-statement cross-check.

Dataset quarter labels are filing-package labels. REPORT_DATE is the portfolio date; REPORT_ENDING_PERIOD is fiscal year-end. These dates must not be substituted for each other.

## Monthly fund flows

| Month | Subscriptions excluding reinvestment (USD) | Reinvestments (USD) | Redemptions (USD) | Net-outflow proxy (USD) |
|---|---:|---:|---:|---:|
| 2026-04 | 7,691,557.95 | 0.00 | 11,109,734.85 | 3,418,176.90 |
| 2026-05 | 9,374,597.08 | 0.00 | 13,647,557.70 | 4,272,960.62 |
| 2026-06 | 31,849,196.02 | 58,917,715.84 | 41,481,124.53 | 9,631,928.51 |

Net outflow = max(reported redemptions - subscriptions excluding reinvestments, 0). Reinvestments are retained separately and are not subtracted again from SALES_FLOW. This is not confirmed cash demand. The true cash demand and true available cash columns deliberately remain blank. Reported-end NAV ratios are labeled descriptive-only; earlier publicly available NAV is required for ex-ante normalization.

## Data sources and additional needs

| Source | Provides | Additional data needed |
|---|---|---|
| [SEC N-PORT extracted files](https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets) | NAV, B.2.f cash, B.6 flows, returns, holdings, repo and lending fields | Earlier filings, daily cash demands and actual execution records |
| [SEC Form N-PORT / dictionary](https://www.sec.gov/files/nport_readme.pdf) | Field definitions and join keys | Use https://www.sec.gov/files/formn-port.pdf for form instructions |
| [Clipper regulatory documents](https://clipperfund.com/resources/regulatory-documents) | Financial statements, strategy and redemption arrangements | Receivable/other-asset reconciliation, collateral and in-kind/ReFlow details |
| [FRED DGS3MO](https://fred.stlouisfed.org/series/DGS3MO) | Cash-yield proxy | Align horizon and estimate equity-over-cash premium |
| [FRED VIXCLS](https://fred.stlouisfed.org/series/VIXCLS) | Market-stress indicator | Asset-specific liquidity measures |
| [WRDS / CRSP](https://wrds-www.wharton.upenn.edu/pages/about/data-vendors/center-for-research-in-security-prices-crsp/) | Price, return and volume data if licensed | Confirm access and foreign-security coverage; market impact still requires estimation |

## Data-quality findings

Failed checks: 0. Review flags: 7.
- **WARN: Holding percentage vs USD value / NAV**. Maximum per-holding difference: 0.0000239490 percentage points Preserve reported and recomputed weights; review small differences without rewriting the source.
- **WARN: Holdings and residual cash vs total assets**. Other-asset residuals USD: [2698021.25] Receivables/other balance-sheet items may explain this; it is not automatically a missing-holdings error.
- **WARN: Monthly history**. 3 months; 1 report dates Fewer than 24 months is a research-planning flag, not a guarantee of adequacy above that threshold. Avoid tail-risk estimation from three months.
- **WARN: NAV available before month-start**. 3/3 flow rows lack an earlier publicly available NAV. Leave ex-ante flow rates blank; report-end NAV ratios are descriptive only.
- **WARN: Actual cash demands and intramonth timing**. Monthly B.6 aggregates only. In-kind activity, ReFlow, payment sequencing and other cash obligations are not identified.
- **WARN: Verified available cash**. Cash-like assets are candidates; true available cash is not inferred. Verify collateral, maturity, restrictions and settlement against fund statements before model calibration.
- **WARN: Restricted securities**. 1 holdings have IS_RESTRICTED_SECURITY=Y. Do not assign all equities the same sale-capacity assumption; review these positions separately.

## Coverage and next work

Within the requested historical target 2020-01 to 2025-12, 0/72 monthly observations are present. This target is configurable; observations outside it are retained in the data outputs.

Collect earlier N-PORT packages before estimating redemption distributions or running chronological train/test comparisons. Review financial statements for receivables, restricted balances, in-kind redemptions and ReFlow. Add price/volume and cash-yield inputs. Unobserved execution costs, cash-redemption fractions and sale capacity may initially use clearly labeled dummy inputs followed by sensitivity analysis; this extraction script fabricates none of them.

## Reproducibility

All raw source files are read-only inputs. source_inventory.csv records paths, sizes and row counts. All accession versions are retained in filings.csv, snapshots_all_versions.csv and monthly_flows_all_versions.csv. Descriptive outputs choose the latest filing per report date. For historical decisions, run with --as-of and use availability dates; a date-only filing is conservatively available on the next calendar day. Identifier rows are aggregated before joining; repo collateral assets are never added again as portfolio holdings.

AI assistance: OpenAI Codex assisted with coding and field interpretation. The project team remains responsible for the submitted analysis.
