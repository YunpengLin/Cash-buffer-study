
#!/usr/bin/env python3
"""Offline SEC N-PORT extraction and Phase 1 data exploration for Clipper Fund.

Run from the project root:
    python scripts/clipper_phase1.py --data-dir data --output-dir output/clipper_phase1

Requires Python 3.10+ and pandas. Large TSVs are read in chunks. Source files are
never changed. Add more extracted quarterly folders under data/ and rerun.
This script prepares evidence and checks; it does not estimate an optimal buffer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SERIES_ID = "S000011372"
CLASS_ID = "C000031492"
META = ["SOURCE_DATASET", "SOURCE_FILE"]
DATE_COLS = ["FILING_DATE", "REPORT_DATE", "REPORT_ENDING_PERIOD"]
FUND_NUMBERS = ["TOTAL_ASSETS", "TOTAL_LIABILITIES", "NET_ASSETS",
                "CASH_NOT_RPTD_IN_C_OR_D", "ASSETS_ATTRBT_TO_MISC_SECURITY"]
FLOW_NUMBERS = [f"{name}_FLOW_MON{i}" for i in (1, 2, 3)
                for name in ("SALES", "REINVESTMENT", "REDEMPTION")]


def dates(values: pd.Series) -> pd.Series:
    """SEC's DD-MON-YYYY, independent of the computer's date/locale settings."""
    text = values.astype(str).str.strip().str.upper()
    result = pd.to_datetime(text, format="%d-%b-%Y", errors="coerce")
    iso = pd.to_datetime(text, format="%Y-%m-%d", errors="coerce")
    return result.fillna(iso)


def require(frame: pd.DataFrame, columns: list[str], context: str) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"{context}: missing required columns {missing}")


def number(frame: pd.DataFrame, columns: list[str]) -> None:
    """Keep empty/N/A values missing; a true zero remains zero."""
    for col in columns:
        if col in frame:
            original = frame[col].fillna("").astype(str).str.strip()
            converted = pd.to_numeric(original, errors="coerce")
            bad = ~original.str.upper().isin(["", "N/A", "NA", "NAN", "NONE"]) & converted.isna()
            if bad.any():
                raise ValueError(f"{col}: invalid numeric values {original[bad].unique()[:5]}")
            frame[col] = converted


def distinct(frame: pd.DataFrame, key: list[str], context: str) -> pd.DataFrame:
    """Collapse identical re-exports, but fail on conflicting versions of a key."""
    if frame.empty:
        return frame.copy()
    business = [c for c in frame if c not in META]
    reduced = frame.drop_duplicates(business).copy()
    duplicate = reduced.duplicated(key, keep=False)
    if duplicate.any():
        example = reduced.loc[duplicate, key].head(5).to_dict("records")
        raise ValueError(f"{context}: conflicting duplicate keys {example}")
    return reduced


def read_selected(folder: Path, table: str, key: str, selected: set[str],
                  chunksize: int, inventory: list[dict], required: bool = True) -> pd.DataFrame:
    path = folder / f"{table}.tsv"
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return pd.DataFrame(columns=[key, *META])
    header = pd.read_csv(path, sep="\t", dtype=str, nrows=0).columns.tolist()
    if key not in header:
        raise ValueError(f"{path}: missing key {key}")
    pieces, scanned = [], 0
    for chunk in pd.read_csv(path, sep="\t", dtype=str, na_filter=False,
                             chunksize=chunksize, encoding="utf-8-sig"):
        scanned += len(chunk)
        part = chunk.loc[chunk[key].str.strip().isin(selected)].copy()
        if not part.empty:
            pieces.append(part)
    result = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(columns=header)
    result["SOURCE_DATASET"] = str(folder.resolve())
    result["SOURCE_FILE"] = str(path.resolve())
    inventory.append({"table": table, "path": str(path.resolve()),
                      "bytes": path.stat().st_size, "rows_scanned": scanned,
                      "selected_rows": len(result)})
    return result


def aggregate_identifiers(frame: pd.DataFrame) -> pd.DataFrame:
    """One-to-many security identifiers must not multiply holding values."""
    columns = ["IDENTIFIER_ISIN", "IDENTIFIER_TICKER", "OTHER_IDENTIFIER", "OTHER_IDENTIFIER_DESC"]
    result_columns = ["SOURCE_DATASET", "HOLDING_ID", *columns, "IDENTIFIER_ROW_COUNT"]
    if frame.empty:
        return pd.DataFrame(columns=result_columns)
    require(frame, columns, "IDENTIFIERS")

    def combine(values: pd.Series) -> str:
        return " | ".join(sorted({v.strip() for v in values.astype(str) if v.strip()}))

    grouped = frame.groupby(["SOURCE_DATASET", "HOLDING_ID"], sort=False)
    result = grouped[columns].agg(combine).reset_index()
    counts = grouped.size().rename("IDENTIFIER_ROW_COUNT").reset_index()
    return result.merge(counts, on=["SOURCE_DATASET", "HOLDING_ID"], validate="one_to_one")


def add_public_nav(flows: pd.DataFrame, snapshots: pd.DataFrame) -> pd.DataFrame:
    """Attach only a NAV snapshot disclosed before each month-start decision.

    The filing has only a date, not a release timestamp. We conservatively make
    it available the next calendar day. An amendment only replaces a snapshot
    from its own availability date onward; older report dates cannot replace a
    newer available report. This is a scale proxy, not true monthly opening NAV.
    """
    result = flows.copy()
    result["LAGGED_PUBLIC_NAV_USD"] = float("nan")
    result["LAGGED_PUBLIC_NAV_REPORT_DATE"] = pd.NaT
    result["LAGGED_PUBLIC_NAV_FILING_DATE"] = pd.NaT
    result["LAGGED_PUBLIC_NAV_ACCESSION"] = ""
    for idx, row in result.iterrows():
        candidates = snapshots.loc[snapshots["AVAILABLE_FROM_DATE"] <= row["DECISION_DATE"]]
        if candidates.empty:
            continue
        chosen = candidates.sort_values(["REPORT_DATE", "FILING_DATE", "ACCESSION_NUMBER"]).iloc[-1]
        if pd.notna(chosen["NET_ASSETS"]) and chosen["NET_ASSETS"] > 0:
            result.loc[idx, "LAGGED_PUBLIC_NAV_USD"] = chosen["NET_ASSETS"]
            result.loc[idx, "LAGGED_PUBLIC_NAV_REPORT_DATE"] = chosen["REPORT_DATE"]
            result.loc[idx, "LAGGED_PUBLIC_NAV_FILING_DATE"] = chosen["FILING_DATE"]
            result.loc[idx, "LAGGED_PUBLIC_NAV_ACCESSION"] = chosen["ACCESSION_NUMBER"]
    result["NET_OUTFLOW_RATE_LAGGED_PUBLIC_NAV"] = (
        result["NET_OUTFLOW_PROXY_USD"] / result["LAGGED_PUBLIC_NAV_USD"])
    # Date-only convention. Intraday or daily-payment models need better timestamps.
    return result


def make_flows(fund: pd.DataFrame, returns: pd.DataFrame, class_id: str) -> pd.DataFrame:
    wanted = returns.loc[returns["CLASS_ID"].eq(class_id)].copy()
    wanted = distinct(wanted, ["ACCESSION_NUMBER", "CLASS_ID"], "monthly returns")
    return_map = wanted.set_index("ACCESSION_NUMBER").to_dict("index")
    records = []
    for _, snap in fund.iterrows():
        period = snap["REPORT_DATE"].to_period("M")
        for slot in (1, 2, 3):
            month = period - (3 - slot)
            raw_return = return_map.get(snap["ACCESSION_NUMBER"], {}).get(f"MONTHLY_TOTAL_RETURN{slot}", "")
            return_pct = pd.to_numeric(raw_return, errors="coerce")
            sales = snap[f"SALES_FLOW_MON{slot}"]
            redeemed = snap[f"REDEMPTION_FLOW_MON{slot}"]
            net_outflow = redeemed - sales
            records.append({
                "ACCESSION_NUMBER": snap["ACCESSION_NUMBER"], "SERIES_ID": snap["SERIES_ID"],
                "CLASS_ID": class_id, "MONTH": str(month), "DECISION_DATE": month.start_time,
                "REPORT_DATE": snap["REPORT_DATE"], "FILING_DATE": snap["FILING_DATE"],
                "MONTH_SLOT": slot, "SALES_EX_REINVESTMENT_USD": sales,
                "REINVESTMENT_USD": snap[f"REINVESTMENT_FLOW_MON{slot}"],
                "REDEMPTION_REPURCHASE_USD": redeemed,
                "NET_SUBSCRIPTION_PROXY_USD": sales - redeemed,
                "NET_OUTFLOW_PROXY_USD": max(net_outflow, 0) if pd.notna(net_outflow) else float("nan"),
                "REPORT_END_NAV_USD": snap["NET_ASSETS"],
                "NET_OUTFLOW_RATE_REPORT_END_NAV_DESCRIPTIVE_ONLY":
                    max(net_outflow, 0) / snap["NET_ASSETS"]
                    if pd.notna(net_outflow) and snap["NET_ASSETS"] > 0 else float("nan"),
                "FUND_RETURN_PERCENT": return_pct, "FUND_RETURN_DECIMAL": return_pct / 100,
                "ACTUAL_NET_CASH_DEMAND_USD": float("nan"),
                "IS_LATEST_REPORT_VERSION": snap["IS_LATEST_REPORT_VERSION"],
                "SOURCE_FILE": snap["SOURCE_FILE"],
            })
    all_flows = pd.DataFrame(records)
    return add_public_nav(all_flows, fund)


def build_holding_features(holdings: pd.DataFrame, snapshots: pd.DataFrame) -> pd.DataFrame:
    output = holdings.copy()
    require(output, ["CURRENCY_VALUE", "PERCENTAGE", "ASSET_CAT", "PAYOFF_PROFILE",
                     "IS_RESTRICTED_SECURITY"], "FUND_REPORTED_HOLDING")
    number(output, ["CURRENCY_VALUE", "PERCENTAGE", "BALANCE", "EXCHANGE_RATE",
                    "CASH_COLLATERAL_AMOUNT", "NON_CASH_COLLATERAL_VALUE", "REPURCHASE_RATE"])
    # Form C.2 value is already USD, even for a holding denominated in another currency.
    output["VALUE_USD"] = output["CURRENCY_VALUE"]
    output["REPORTED_NAV_WEIGHT_DECIMAL"] = output["PERCENTAGE"] / 100
    info = snapshots[["ACCESSION_NUMBER", "REPORT_DATE", "FILING_DATE", "NET_ASSETS",
                      "IS_LATEST_REPORT_VERSION"]]
    output = output.merge(info, on="ACCESSION_NUMBER", validate="many_to_one")
    output["COMPUTED_NAV_WEIGHT_DECIMAL"] = output["VALUE_USD"] / output["NET_ASSETS"]
    output["WEIGHT_DIFFERENCE_PERCENTAGE_POINTS"] = (
        output["PERCENTAGE"] - output["COMPUTED_NAV_WEIGHT_DECIMAL"] * 100)
    output["REPO_MATURITY_DATE"] = dates(output["MATURITY_DATE"])
    output["REPO_DAYS_TO_MATURITY"] = (output["REPO_MATURITY_DATE"] - output["REPORT_DATE"]).dt.days
    for flag, amount, derived in (
        ("IS_CASH_COLLATERAL", "CASH_COLLATERAL_AMOUNT", "KNOWN_CASH_COLLATERAL_USD"),
        ("IS_NON_CASH_COLLATERAL", "NON_CASH_COLLATERAL_VALUE", "KNOWN_NONCASH_COLLATERAL_USD"),
    ):
        if flag not in output:
            output[flag] = ""
        if amount not in output:
            output[amount] = float("nan")
        collateral_flag = output[flag].fillna("")
        output[derived] = output[amount].mask(collateral_flag.eq("N"), 0).where(
            collateral_flag.isin(["Y", "N"]))
    known_collateral = output["KNOWN_CASH_COLLATERAL_USD"] + output["KNOWN_NONCASH_COLLATERAL_USD"]
    output["UNRESTRICTED_NONCOLLATERAL_VALUE_PROXY_USD"] = (
        output["VALUE_USD"] - known_collateral).clip(lower=0)
    output.loc[output["IS_RESTRICTED_SECURITY"].eq("Y"),
               "UNRESTRICTED_NONCOLLATERAL_VALUE_PROXY_USD"] = 0
    output.loc[~output["IS_RESTRICTED_SECURITY"].isin(["Y", "N"]),
               "UNRESTRICTED_NONCOLLATERAL_VALUE_PROXY_USD"] = float("nan")
    output["IS_CASHLIKE_CANDIDATE"] = (
        output["ASSET_CAT"].isin(["RA", "STIV"]) & output["PAYOFF_PROFILE"].eq("Long"))
    output["CASHLIKE_ASSET_PROXY_USD"] = output["UNRESTRICTED_NONCOLLATERAL_VALUE_PROXY_USD"].where(
        output["IS_CASHLIKE_CANDIDATE"], 0)
    output["CASHLIKE_REVIEW_NOTE"] = ""
    output.loc[output["ASSET_CAT"].eq("RA"), "CASHLIKE_REVIEW_NOTE"] = (
        "Repo candidate: verify maturity, transaction direction, settlement and payment availability.")
    output.loc[output["ASSET_CAT"].eq("STIV"), "CASHLIKE_REVIEW_NOTE"] = (
        "Short-term vehicle candidate: verify withdrawals, restrictions and payment timing.")
    return output


def add_summary(fund: pd.DataFrame, holdings: pd.DataFrame) -> pd.DataFrame:
    fund = fund.copy()
    for idx, row in fund.iterrows():
        h = holdings.loc[holdings["ACCESSION_NUMBER"].eq(row["ACCESSION_NUMBER"])]
        value = h["VALUE_USD"].sum(min_count=1)
        candidates = h.loc[h["IS_CASHLIKE_CANDIDATE"], "CASHLIKE_ASSET_PROXY_USD"]
        candidate_value = (candidates.sum() if not candidates.isna().any() else float("nan"))
        residual_cash = row["CASH_NOT_RPTD_IN_C_OR_D"]
        entries = {
            "HOLDING_COUNT": len(h), "EQUITY_HOLDING_COUNT": int(h["ASSET_CAT"].eq("EC").sum()),
            "HOLDINGS_VALUE_USD": value,
            "REPORTED_HOLDINGS_WEIGHT_PERCENT": h["PERCENTAGE"].sum(min_count=1),
            "CASHLIKE_HOLDINGS_PROXY_USD": candidate_value,
            "CASH_PLUS_CASHLIKE_PROXY_USD": residual_cash + candidate_value,
            "CASH_PLUS_CASHLIKE_PROXY_RATIO": (residual_cash + candidate_value) / row["NET_ASSETS"],
            "ACCOUNTING_IDENTITY_RESIDUAL_USD": row["TOTAL_ASSETS"] - row["TOTAL_LIABILITIES"] - row["NET_ASSETS"],
            "OTHER_ASSETS_RESIDUAL_USD": row["TOTAL_ASSETS"] - value - residual_cash,
            "MAX_ABS_HOLDING_WEIGHT_DIFF_PP": h["WEIGHT_DIFFERENCE_PERCENTAGE_POINTS"].abs().max(),
            "TRUE_AVAILABLE_CASH_USD": float("nan"),
        }
        for col, val in entries.items():
            fund.loc[idx, col] = val
    return fund


def quality_checks(fund: pd.DataFrame, holdings: pd.DataFrame, flows: pd.DataFrame,
                   optional_missing: list[str]) -> pd.DataFrame:
    checks = []

    def check(name: str, status: str, evidence: str, implication: str) -> None:
        checks.append({"check": name, "status": status, "evidence": evidence, "implication": implication})

    check("NAV accounting identity", "PASS" if fund.ACCOUNTING_IDENTITY_RESIDUAL_USD.abs().le(0.01).all() else "FAIL",
          f"Maximum residual USD: {fund.ACCOUNTING_IDENTITY_RESIDUAL_USD.abs().max():.4f}",
          "TOTAL_ASSETS - TOTAL_LIABILITIES must equal NET_ASSETS.")
    check("Positive NAV", "PASS" if fund.NET_ASSETS.gt(0).all() else "FAIL", str(fund.NET_ASSETS.tolist()),
          "Non-positive or missing NAV invalidates ratio calculations.")
    check("Holdings key uniqueness", "PASS" if not holdings.duplicated(["ACCESSION_NUMBER", "HOLDING_ID"]).any() else "FAIL",
          f"{len(holdings)} rows", "Identifiers must not duplicate holding market values.")
    check("Required values present", "PASS" if not (holdings.VALUE_USD.isna().any() or flows.SALES_EX_REINVESTMENT_USD.isna().any()
          or flows.REDEMPTION_REPURCHASE_USD.isna().any()) else "FAIL", "Holding values and subscription/redemption fields checked.",
          "Missing input values remain missing and must not become zero.")
    difference = holdings.WEIGHT_DIFFERENCE_PERCENTAGE_POINTS.abs().max()
    check("Holding percentage vs USD value / NAV", "PASS" if difference <= 0.000001 else "WARN",
          f"Maximum per-holding difference: {difference:.10f} percentage points",
          "Preserve reported and recomputed weights; review small differences without rewriting the source.")
    check("Holdings and residual cash vs total assets", "WARN" if fund.OTHER_ASSETS_RESIDUAL_USD.abs().gt(1).any() else "PASS",
          f"Other-asset residuals USD: {fund.OTHER_ASSETS_RESIDUAL_USD.round(2).tolist()}",
          "Receivables/other balance-sheet items may explain this; it is not automatically a missing-holdings error.")
    check("Monthly history", "WARN" if flows.MONTH.nunique() < 24 else "PASS",
          f"{flows.MONTH.nunique()} months; {fund.REPORT_DATE.nunique()} report dates",
          "Fewer than 24 months is a research-planning flag, not a guarantee of adequacy above that threshold. Avoid tail-risk estimation from three months.")
    absent = int(flows.LAGGED_PUBLIC_NAV_USD.isna().sum())
    check("NAV available before month-start", "WARN" if absent else "PASS",
          f"{absent}/{len(flows)} flow rows lack an earlier publicly available NAV.",
          "Leave ex-ante flow rates blank; report-end NAV ratios are descriptive only.")
    check("Actual cash demands and intramonth timing", "WARN", "Monthly B.6 aggregates only.",
          "In-kind activity, ReFlow, payment sequencing and other cash obligations are not identified.")
    check("Verified available cash", "WARN", "Cash-like assets are candidates; true available cash is not inferred.",
          "Verify collateral, maturity, restrictions and settlement against fund statements before model calibration.")
    restricted = int(holdings.IS_RESTRICTED_SECURITY.eq("Y").sum())
    if restricted:
        check("Restricted securities", "WARN", f"{restricted} holdings have IS_RESTRICTED_SECURITY=Y.",
              "Do not assign all equities the same sale-capacity assumption; review these positions separately.")
    if optional_missing:
        check("Optional supporting files", "WARN", " | ".join(optional_missing),
              "Related outputs remain unknown rather than assuming absence of collateral/identifiers.")
    return pd.DataFrame(checks)


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig", date_format="%Y-%m-%d", float_format="%.12g", na_rep="")


def source_table(folders: list[Path]) -> pd.DataFrame:
    return pd.DataFrame([
        {"source": "SEC N-PORT extracted files", "link": "https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets",
         "local_evidence": " | ".join(str(p.resolve()) for p in folders), "provides": "NAV, B.2.f cash, B.6 flows, returns, holdings, repo and lending fields",
         "additional_data_needed": "Earlier filings, daily cash demands and actual execution records", "availability": "Local files read; remote links not fetched by this offline script"},
        {"source": "SEC Form N-PORT / dictionary", "link": "https://www.sec.gov/files/nport_readme.pdf",
         "local_evidence": "nport_metadata.json and nport_readme.htm", "provides": "Field definitions and join keys",
         "additional_data_needed": "Use https://www.sec.gov/files/formn-port.pdf for form instructions", "availability": "Local metadata used"},
        {"source": "Clipper regulatory documents", "link": "https://clipperfund.com/resources/regulatory-documents",
         "local_evidence": "Not imported by this script", "provides": "Financial statements, strategy and redemption arrangements",
         "additional_data_needed": "Receivable/other-asset reconciliation, collateral and in-kind/ReFlow details", "availability": "Public; manual review needed"},
        {"source": "FRED DGS3MO", "link": "https://fred.stlouisfed.org/series/DGS3MO", "local_evidence": "Not imported",
         "provides": "Cash-yield proxy", "additional_data_needed": "Align horizon and estimate equity-over-cash premium", "availability": "Public; future model input"},
        {"source": "FRED VIXCLS", "link": "https://fred.stlouisfed.org/series/VIXCLS", "local_evidence": "Not imported",
         "provides": "Market-stress indicator", "additional_data_needed": "Asset-specific liquidity measures", "availability": "Public; future model input"},
        {"source": "WRDS / CRSP", "link": "https://wrds-www.wharton.upenn.edu/pages/about/data-vendors/center-for-research-in-security-prices-crsp/",
         "local_evidence": "Not imported", "provides": "Price, return and volume data if licensed",
         "additional_data_needed": "Confirm access and foreign-security coverage; market impact still requires estimation", "availability": "Subscription; access unconfirmed"},
    ])


def write_report(output: Path, fund: pd.DataFrame, flows: pd.DataFrame,
                 checks: pd.DataFrame, coverage: pd.DataFrame, sources: pd.DataFrame) -> None:
    latest = fund.sort_values(["REPORT_DATE", "FILING_DATE"]).iloc[-1]
    failures = checks.loc[checks.status.eq("FAIL")]
    warnings = checks.loc[checks.status.eq("WARN")]
    lines = ["# Clipper Fund: Phase 1 Data Exploration", "",
        "## Problem definition", "",
        "The fund manager must choose an initial cash buffer and, after investor redemptions occur, which assets to sell. "
        "The proposed two-stage model balances the assumed equity-over-cash opportunity cost with scenario-weighted execution costs. "
        "Sales must respect holdings and sale/settlement capacity, and cash plus proceeds net of costs must cover modeled redemptions. "
        "[Clipper's prospectus](https://clipperfund.com/documents/CFProsp.pdf) describes cash, security sales and other redemption arrangements. "
        "The current files support a portfolio snapshot, not an empirically validated optimal-buffer recommendation.", "",
        "## Observed local coverage", "",
        f"- Fund: {latest['SERIES_NAME']} ({latest['SERIES_ID']}); class CFIMX.",
        f"- Distinct filings: {fund.ACCESSION_NUMBER.nunique()}; report dates: {fund.REPORT_DATE.nunique()}.",
        f"- Latest report date: {latest.REPORT_DATE:%Y-%m-%d}; filed: {latest.FILING_DATE:%Y-%m-%d}.",
        f"- Monthly observations: {flows.MONTH.nunique()} ({flows.MONTH.min()} to {flows.MONTH.max()}).",
        f"- Holdings in latest filing: {int(latest.HOLDING_COUNT)}; common equities: {int(latest.EQUITY_HOLDING_COUNT)}.",
        f"- NAV: USD {latest.NET_ASSETS:,.2f}; B.2.f residual cash: USD {latest.CASH_NOT_RPTD_IN_C_OR_D:,.2f}.",
        f"- Cash plus cash-like asset proxy: USD {latest.CASH_PLUS_CASHLIKE_PROXY_USD:,.2f} "
        f"({latest.CASH_PLUS_CASHLIKE_PROXY_RATIO:.6%} of NAV), conditional on availability assumptions.",
        f"- Other-assets residual: USD {latest.OTHER_ASSETS_RESIDUAL_USD:,.2f}; requires a financial-statement cross-check.",
        "", "Dataset quarter labels are filing-package labels. REPORT_DATE is the portfolio date; "
        "REPORT_ENDING_PERIOD is fiscal year-end. These dates must not be substituted for each other.", "",
        "## Monthly fund flows", "", "| Month | Subscriptions excluding reinvestment (USD) | Reinvestments (USD) | Redemptions (USD) | Net-outflow proxy (USD) |", "|---|---:|---:|---:|---:|"]
    for _, row in flows.iterrows():
        lines.append(f"| {row.MONTH} | {row.SALES_EX_REINVESTMENT_USD:,.2f} | {row.REINVESTMENT_USD:,.2f} | "
                     f"{row.REDEMPTION_REPURCHASE_USD:,.2f} | {row.NET_OUTFLOW_PROXY_USD:,.2f} |")
    lines += ["", "Net outflow = max(reported redemptions - subscriptions excluding reinvestments, 0). "
        "Reinvestments are retained separately and are not subtracted again from SALES_FLOW. This is not confirmed cash demand. "
        "The true cash demand and true available cash columns deliberately remain blank. "
        "Reported-end NAV ratios are labeled descriptive-only; earlier publicly available NAV is required for ex-ante normalization.",
        "", "## Data sources and additional needs", "", "| Source | Provides | Additional data needed |", "|---|---|---|"]
    for _, row in sources.iterrows():
        lines.append(f"| [{row.source}]({row.link}) | {row.provides} | {row.additional_data_needed} |")
    lines += ["", "## Data-quality findings", "",
              f"Failed checks: {len(failures)}. Review flags: {len(warnings)}."]
    for _, row in checks.loc[checks.status.ne("PASS")].iterrows():
        lines.append(f"- **{row.status}: {row['check']}**. {row.evidence} {row.implication}")
    lines += ["", "## Coverage and next work", "",
        f"Within the requested historical target {coverage.MONTH.min()} to {coverage.MONTH.max()}, "
        f"{int(coverage.OBSERVED.sum())}/{len(coverage)} monthly observations are present. "
        "This target is configurable; observations outside it are retained in the data outputs.", "",
        "Collect earlier N-PORT packages before estimating redemption distributions or running chronological train/test comparisons. "
        "Review financial statements for receivables, restricted balances, in-kind redemptions and ReFlow. "
        "Add price/volume and cash-yield inputs. Unobserved execution costs, cash-redemption fractions and sale capacity "
        "may initially use clearly labeled dummy inputs followed by sensitivity analysis; this extraction script fabricates none of them.",
        "", "## Reproducibility", "",
        "All raw source files are read-only inputs. source_inventory.csv records paths, sizes and row counts. "
        "All accession versions are retained in filings.csv, snapshots_all_versions.csv and monthly_flows_all_versions.csv. "
        "Descriptive outputs choose the latest filing per report date. For historical decisions, run with --as-of and use "
        "availability dates; a date-only filing is conservatively available on the next calendar day. "
        "Identifier rows are aggregated before joining; repo collateral assets are never added again as portfolio holdings.", "",
        "AI assistance: OpenAI Codex assisted with coding and field interpretation. The project team remains responsible for the submitted analysis.", ""]
    (output / "phase1_data_report.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> dict:
    data_dir, output = Path(args.data_dir).resolve(), Path(args.output_dir).resolve()
    if not data_dir.exists():
        raise FileNotFoundError(data_dir)
    if output == data_dir or data_dir in output.parents:
        raise ValueError("Output must be outside the raw data directory.")
    folders = sorted({p.parent for p in data_dir.rglob("FUND_REPORTED_INFO.tsv")})
    if not folders:
        raise ValueError(f"No extracted N-PORT folders found under {data_dir}")
    inventory, optional_missing = [], []
    fund_parts, return_parts, holding_parts, note_parts = [], [], [], []
    for folder in folders:
        print(f"Reading {folder.name} ...", flush=True)
        selected = read_selected(folder, "FUND_REPORTED_INFO", "SERIES_ID", {args.series_id}, args.chunksize, inventory)
        if selected.empty:
            continue
        accession = set(selected.ACCESSION_NUMBER)
        submissions = read_selected(folder, "SUBMISSION", "ACCESSION_NUMBER", accession, args.chunksize, inventory)
        registrants = read_selected(folder, "REGISTRANT", "ACCESSION_NUMBER", accession, args.chunksize, inventory)
        submissions = distinct(submissions, ["ACCESSION_NUMBER"], "SUBMISSION")
        registrants = distinct(registrants, ["ACCESSION_NUMBER"], "REGISTRANT")
        joined = selected.merge(submissions.drop(columns=META), on="ACCESSION_NUMBER", how="left", validate="one_to_one")
        joined = joined.merge(registrants[["ACCESSION_NUMBER", "CIK", "REGISTRANT_NAME"]], on="ACCESSION_NUMBER", how="left", validate="one_to_one")
        require(joined, DATE_COLS + FUND_NUMBERS + FLOW_NUMBERS, "fund snapshot")
        for col in DATE_COLS:
            joined[col] = dates(joined[col])
        if joined[DATE_COLS].isna().any().any() or joined.CIK.isna().any():
            raise ValueError(f"{folder}: missing/invalid submission dates or registrant join")
        # Without acceptance timestamps, use next-day availability conservatively.
        joined["AVAILABLE_FROM_DATE"] = joined.FILING_DATE + pd.Timedelta(days=1)
        if args.as_of:
            joined = joined.loc[joined.AVAILABLE_FROM_DATE <= pd.Timestamp(args.as_of)].copy()
        if joined.empty:
            continue
        accession = set(joined.ACCESSION_NUMBER)
        number(joined, FUND_NUMBERS + FLOW_NUMBERS)
        fund_parts.append(joined)
        return_parts.append(read_selected(folder, "MONTHLY_TOTAL_RETURN", "ACCESSION_NUMBER", accession, args.chunksize, inventory))
        notes = read_selected(folder, "EXPLANATORY_NOTE", "ACCESSION_NUMBER", accession, args.chunksize, inventory, required=False)
        note_parts.append(notes)
        holdings = read_selected(folder, "FUND_REPORTED_HOLDING", "ACCESSION_NUMBER", accession, args.chunksize, inventory)
        if holdings.empty:
            raise ValueError(f"{folder}: Clipper filing exists but holdings are missing")
        ids = set(holdings.HOLDING_ID)
        for table in ("IDENTIFIERS", "SECURITIES_LENDING", "REPURCHASE_AGREEMENT"):
            if not (folder / f"{table}.tsv").exists():
                optional_missing.append(str(folder / f"{table}.tsv"))
            extra = read_selected(folder, table, "HOLDING_ID", ids, args.chunksize, inventory, required=False)
            if table == "IDENTIFIERS":
                extra = aggregate_identifiers(extra)
            elif not extra.empty:
                extra = distinct(extra, ["HOLDING_ID"], table).drop(columns=["SOURCE_FILE"])
            else:
                continue
            holdings = holdings.merge(extra, on=["SOURCE_DATASET", "HOLDING_ID"], how="left", validate="many_to_one")
        for col in ("IS_CASH_COLLATERAL", "CASH_COLLATERAL_AMOUNT", "IS_NON_CASH_COLLATERAL",
                    "NON_CASH_COLLATERAL_VALUE", "MATURITY_DATE", "REPURCHASE_RATE"):
            if col not in holdings:
                holdings[col] = ""
        holding_parts.append(holdings)
    if not fund_parts:
        raise ValueError("No Clipper records available for this Series ID/as-of date.")
    fund = distinct(pd.concat(fund_parts, ignore_index=True), ["ACCESSION_NUMBER"], "fund versions")
    fund = fund.sort_values(["REPORT_DATE", "FILING_DATE", "ACCESSION_NUMBER"]).reset_index(drop=True)
    fund["IS_LATEST_REPORT_VERSION"] = ~fund.duplicated(["SERIES_ID", "REPORT_DATE"], keep="last")
    fund["SEC_FILING_URL"] = fund.apply(lambda r: f"https://www.sec.gov/Archives/edgar/data/{int(r.CIK)}/{r.ACCESSION_NUMBER.replace('-', '')}/{r.ACCESSION_NUMBER}-index.html", axis=1)
    returns = distinct(pd.concat(return_parts, ignore_index=True), ["ACCESSION_NUMBER", "CLASS_ID"], "returns")
    holdings = distinct(pd.concat(holding_parts, ignore_index=True), ["ACCESSION_NUMBER", "HOLDING_ID"], "holdings")
    holdings = build_holding_features(holdings, fund)
    fund = add_summary(fund, holdings)
    all_flows = make_flows(fund, returns, args.class_id)
    # Overlapping three-month windows are retained above, then resolved descriptively.
    flows = all_flows.sort_values(["MONTH", "REPORT_DATE", "FILING_DATE", "ACCESSION_NUMBER"]).drop_duplicates(["SERIES_ID", "MONTH"], keep="last")
    flows = flows.sort_values("MONTH").reset_index(drop=True)
    checks = quality_checks(fund, holdings, flows, optional_missing)
    expected = pd.period_range(args.expected_start, args.expected_end, freq="M")
    if len(expected) == 0:
        raise ValueError("Expected history end precedes its start.")
    coverage = pd.DataFrame({"MONTH": expected.astype(str)})
    coverage["OBSERVED"] = coverage.MONTH.isin(flows.MONTH)
    sources = source_table(folders)
    output.mkdir(parents=True, exist_ok=True)
    latest_fund = fund.loc[fund.IS_LATEST_REPORT_VERSION].copy()
    latest_holdings = holdings.loc[holdings.IS_LATEST_REPORT_VERSION].copy()
    filings_columns = ["ACCESSION_NUMBER", "SERIES_ID", "CIK", "FILING_DATE", "AVAILABLE_FROM_DATE", "SUB_TYPE",
                       "REPORT_DATE", "REPORT_ENDING_PERIOD", "IS_LATEST_REPORT_VERSION", "SEC_FILING_URL", *META]
    outputs = {
        "filings.csv": fund[filings_columns], "snapshots_all_versions.csv": fund,
        "fund_snapshots.csv": latest_fund, "monthly_flows_all_versions.csv": all_flows,
        "monthly_flows.csv": flows, "holdings.csv": latest_holdings,
        "holdings_all_versions.csv": holdings,
        "cashlike_review.csv": latest_holdings.loc[latest_holdings.IS_CASHLIKE_CANDIDATE],
        "monthly_returns_raw.csv": returns, "quality_checks.csv": checks,
        "expected_history_coverage.csv": coverage, "data_sources.csv": sources,
        "source_inventory.csv": pd.DataFrame(inventory),
        "explanatory_notes.csv": pd.concat(note_parts, ignore_index=True),
    }
    for name, frame in outputs.items():
        write_csv(frame, output / name)
    # Local SEC metadata supplies raw-field meanings; derived meanings are documented here.
    definitions = {}
    for folder in folders:
        metadata = folder / "nport_metadata.json"
        if metadata.exists():
            for table in json.loads(metadata.read_text(encoding="utf-8-sig")).get("tables", []):
                for col in table.get("tableSchema", {}).get("columns", []):
                    definitions.setdefault(col["name"], col.get("dc:description", ""))
    derived = {
        "VALUE_USD": "Reported CURRENCY_VALUE already expressed in USD; no second FX conversion.",
        "NET_OUTFLOW_PROXY_USD": "max(redemptions - sales excluding reinvestments, 0); not verified cash demand.",
        "CASH_PLUS_CASHLIKE_PROXY_USD": "B.2.f cash plus unrestricted non-collateral RA/STIV candidates; availability unverified.",
        "NET_OUTFLOW_RATE_REPORT_END_NAV_DESCRIPTIVE_ONLY": "Outflow / same report's NAV; retrospective scale only; may look ahead.",
        "NET_OUTFLOW_RATE_LAGGED_PUBLIC_NAV": "Outflow / latest NAV publicly available before month-start; blank when absent.",
        "ACTUAL_NET_CASH_DEMAND_USD": "Unavailable from these monthly aggregate records; intentionally blank.",
        "TRUE_AVAILABLE_CASH_USD": "Unavailable until timing/restrictions/obligations verified; intentionally blank.",
        "OTHER_ASSETS_RESIDUAL_USD": "Total assets - holdings value - B.2.f cash; reconcile with financial statements.",
        "AVAILABLE_FROM_DATE": "Filing date + one calendar day; conservative date-only availability convention.",
    }
    dictionary = pd.DataFrame([{"output": name, "column": col, "definition": derived.get(col, definitions.get(col, "Derived/provenance field; see script and README."))}
                               for name, frame in outputs.items() for col in frame])
    write_csv(dictionary, output / "data_dictionary.csv")
    write_report(output, latest_fund, flows, checks, coverage, sources)
    summary = {
        "series_id": args.series_id, "class_id": args.class_id, "filings": len(fund),
        "snapshot_dates": int(fund.REPORT_DATE.nunique()), "months": int(flows.MONTH.nunique()),
        "month_start": str(flows.MONTH.min()), "month_end": str(flows.MONTH.max()),
        "latest_report_date": fund.REPORT_DATE.max().strftime("%Y-%m-%d"),
        "holdings_rows": len(latest_holdings), "failed_checks": int(checks.status.eq("FAIL").sum()),
        "review_flags": int(checks.status.eq("WARN").sum()), "output_dir": str(output),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "arguments": vars(args), "remote_data_downloaded": False,
    }
    (output / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--output-dir", default=str(ROOT / "output" / "clipper_phase1"))
    parser.add_argument("--series-id", default=SERIES_ID)
    parser.add_argument("--class-id", default=CLASS_ID)
    parser.add_argument("--chunksize", type=int, default=100_000)
    parser.add_argument("--as-of", help="ISO date; include only filings conservatively available by this date")
    parser.add_argument("--expected-start", default="2020-01", help="Expected historical month, YYYY-MM")
    parser.add_argument("--expected-end", default="2025-12", help="Expected historical month, YYYY-MM")
    args = parser.parse_args()
    if args.chunksize <= 0:
        parser.error("--chunksize must be positive")
    try:
        summary = run(args)
    except (ValueError, FileNotFoundError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary["failed_checks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
