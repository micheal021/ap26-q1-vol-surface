from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
CLEAN = ROOT / "data" / "clean"
CONTRACTS = ["io2310", "io2311", "io2312", "io2401", "io2402", "io2403", "io2511", "io2512", "io2601", "io2602"]
CALENDAR_START = pd.Timestamp("2023-10-23")
CALENDAR_END = CALENDAR_START + pd.DateOffset(months=1)
SHIBOR_TENORS = {
    "O/N": 1, "1W": 7, "2W": 14, "1M": 30, "3M": 91,
    "6M": 182, "9M": 274, "1Y": 365,
}


def third_friday(year: int, month: int) -> date:
    month_calendar = calendar.monthcalendar(year, month)
    fridays = [week[calendar.FRIDAY] for week in month_calendar if week[calendar.FRIDAY]]
    return date(year, month, fridays[2])


def expiry_from_base(base: str) -> date:
    return third_friday(2000 + int(base[2:4]), int(base[4:6]))


def numeric(value) -> float:
    return float(pd.to_numeric(value, errors="coerce"))


def load_or_fetch(path: Path, fetcher):
    if path.exists():
        return pd.read_csv(path)
    frame = fetcher()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    time.sleep(0.25)
    return frame


def select_strikes(spot: pd.DataFrame, limit: int) -> pd.DataFrame:
    columns = ["行权价", "看涨合约-标识", "看跌合约-标识"]
    selected = spot[columns].copy()
    selected["行权价"] = pd.to_numeric(selected["行权价"], errors="coerce")
    selected = selected.dropna().sort_values("行权价")
    if len(selected) <= limit:
        return selected
    positions = np.linspace(0, len(selected) - 1, limit).round().astype(int)
    return selected.iloc[sorted(set(positions))]


def fetch_contract_quotes(base: str, strikes_per_expiry: int, sleep_seconds: float) -> tuple[pd.DataFrame, list[str]]:
    import akshare as ak

    spot_path = RAW / f"spot_{base}.csv"
    spot = load_or_fetch(spot_path, lambda: ak.option_cffex_hs300_spot_sina(symbol=base))
    selected = select_strikes(spot, strikes_per_expiry)
    expiry = expiry_from_base(base)
    records: list[pd.DataFrame] = []
    symbols: list[str] = []
    for _, row in selected.iterrows():
        for option_type, symbol_column in (("call", "看涨合约-标识"), ("put", "看跌合约-标识")):
            symbol = str(row[symbol_column])
            symbols.append(symbol)
            raw_path = RAW / f"daily_{symbol}.csv"
            try:
                daily = load_or_fetch(raw_path, lambda symbol=symbol: ak.option_cffex_hs300_daily_sina(symbol=symbol))
            except Exception as error:
                print(f"skip {symbol}: {type(error).__name__}: {error}")
                continue
            if daily.empty:
                continue
            daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
            daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
            daily["volume"] = pd.to_numeric(daily["volume"], errors="coerce")
            daily = daily.dropna(subset=["date", "close"])
            first_date = daily["date"].min()
            daily = daily[daily["date"] < first_date + pd.DateOffset(months=1)].copy()
            daily["contract_base"] = base
            daily["contract_symbol"] = symbol
            daily["option_type"] = option_type
            daily["strike"] = numeric(row["行权价"])
            daily["expiry"] = expiry.isoformat()
            records.append(daily[["date", "contract_base", "contract_symbol", "option_type", "strike", "expiry", "close", "volume"]])
            time.sleep(sleep_seconds)
    return (pd.concat(records, ignore_index=True) if records else pd.DataFrame()), symbols


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def supplement_calendar(options: pd.DataFrame, underlying: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    urls = {}
    coverage = {}
    frames = []
    trade_dates = pd.to_datetime(underlying["date"])
    trade_dates = trade_dates[trade_dates.between(CALENDAR_START, CALENDAR_END, inclusive="left")]
    if len(trade_dates) < 15:
        raise ValueError("The underlying calendar does not span a full trading month")
    for day in sorted(trade_dates.unique()):
        day = pd.Timestamp(day)
        label = day.strftime("%Y-%m-%d")
        ymd = day.strftime("%Y%m%d")
        urls[label] = f"http://www.cffex.com.cn/sj/hqsj/rtj/{day:%Y%m/%d}/index.xml"
        cached = RAW / "cffex" / f"{label}.xml"
        if not cached.exists():
            response = subprocess.run(
                ["curl", "--fail", "--silent", "--show-error", "--retry", "2", "--max-time", "30", urls[label]],
                check=True, capture_output=True,
            ).stdout
            ET.fromstring(response)
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(response)

        rows = []
        for record in ET.parse(cached).iterfind("dailydata"):
            if record.findtext("productid") != "IO":
                continue
            if record.findtext("tradingday") != ymd:
                raise ValueError(f"Incorrect trade date returned for {label}")
            base, flag, strike = record.findtext("instrumentid").split("-")
            expiry = datetime.strptime(record.findtext("expiredate"), "%Y%m%d").date()
            close = numeric(record.findtext("closeprice"))
            volume = numeric(record.findtext("volume"))
            if not np.isfinite(close):
                continue
            rows.append({
                "date": label, "contract_base": base.lower(), "contract_symbol": f"{base.lower()}{flag}{strike}",
                "option_type": {"C": "call", "P": "put"}[flag], "strike": float(strike),
                "expiry": expiry.isoformat(), "close": close, "volume": volume,
            })
        frame = pd.DataFrame(rows, columns=options.columns)
        paired = frame.groupby(["expiry", "strike"])["option_type"].nunique().eq(2)
        expiries = sorted(paired[paired].index.get_level_values("expiry").unique())
        if len(expiries) < 4:
            raise ValueError(f"{label} has fewer than four expiries with call/put pairs")
        coverage[label] = expiries
        frames.append(frame)
    common = sorted(set.intersection(*(set(expiries) for expiries in coverage.values())))
    if len(common) < 4:
        raise ValueError(f"Expected four common expiries over the full month, found {common}")
    outside = options[~options["date"].isin(coverage)]
    combined = pd.concat([outside, *frames], ignore_index=True)
    combined = combined.sort_values(["date", "expiry", "strike", "option_type"])
    return combined, {
        "calendar_block": coverage, "common_expiries": common, "calendar_sources": urls,
        "calendar_range": [CALENDAR_START.date().isoformat(), CALENDAR_END.date().isoformat()],
    }


def write_snapshot_csv(frame: pd.DataFrame, path: Path) -> None:
    path.write_bytes(frame.to_csv(index=False).replace("\r\n", "\n").encode("utf-8"))


def write_metadata(metadata: dict) -> None:
    for filename in ["option_quotes.csv", "underlying.csv", "rates.csv"]:
        path = CLEAN / filename
        metadata["artifacts"][f"data/clean/{filename}"] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    (CLEAN / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--strikes-per-expiry", type=int, default=13)
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument("--refresh", action="store_true", help="Retrieve market data instead of verifying the offline snapshot")
    parser.add_argument("--augment-calendar", action="store_true", help="Add the exchange's complete month to the committed snapshot")
    args = parser.parse_args()
    if args.augment_calendar:
        metadata = json.loads((CLEAN / "metadata.json").read_text(encoding="utf-8"))
        options = pd.read_csv(CLEAN / "option_quotes.csv")
        options, calendar_details = supplement_calendar(options, pd.read_csv(CLEAN / "underlying.csv"))
        write_snapshot_csv(options, CLEAN / "option_quotes.csv")
        metadata.update(calendar_details, quote_rows=len(options), expiry_count=int(options["expiry"].nunique()), sample_date_count=int(options["date"].nunique()))
        metadata["calendar_retrieved_at"] = datetime.now().astimezone().isoformat()
        metadata["calendar_source"] = "CFFEX public daily XML closing prices and volumes"
        metadata["sample_rule"] = "Use the exchange daily chains in the shared calendar month and the original Sina samples outside the block."
        write_metadata(metadata)
        print(f"Verified {len(calendar_details['calendar_block'])} daily chains and common expiries: {calendar_details['common_expiries']}")
        return
    if not args.refresh:
        metadata = json.loads((CLEAN / "metadata.json").read_text(encoding="utf-8"))
        for filename, info in metadata["artifacts"].items():
            payload = (ROOT / filename).read_bytes()
            hashes = {hashlib.sha256(value).hexdigest() for value in (payload, payload.replace(b"\r\n", b"\n"))}
            if info["sha256"] not in hashes:
                raise ValueError(f"Snapshot checksum mismatch: {filename}")
        print("Verified committed offline snapshot checksums.")
        return

    import akshare as ak

    np.random.seed(args.seed)
    RAW.mkdir(parents=True, exist_ok=True)
    CLEAN.mkdir(parents=True, exist_ok=True)
    option_frames = []
    symbols: list[str] = []
    for base in CONTRACTS:
        print(f"fetching {base}")
        frame, contract_symbols = fetch_contract_quotes(base, args.strikes_per_expiry, args.sleep)
        if not frame.empty:
            option_frames.append(frame)
        symbols.extend(contract_symbols)
    if not option_frames:
        raise RuntimeError("No historical option quotes were retrieved")
    options = pd.concat(option_frames, ignore_index=True)
    options["date"] = pd.to_datetime(options["date"]).dt.strftime("%Y-%m-%d")
    options = options.sort_values(["date", "expiry", "strike", "option_type"])
    options_path = CLEAN / "option_quotes.csv"
    min_date = options["date"].min()
    max_date = options["date"].max()
    min_date_dt = pd.to_datetime(min_date)
    max_date_dt = pd.to_datetime(max_date)
    underlying_path = RAW / "underlying_csi300.csv"
    underlying = load_or_fetch(underlying_path, lambda: ak.stock_zh_index_daily(symbol="sh000300"))
    underlying["date"] = pd.to_datetime(underlying["date"], errors="coerce")
    underlying["close"] = pd.to_numeric(underlying["close"], errors="coerce")
    underlying = underlying.dropna(subset=["date", "close"])
    options, calendar_details = supplement_calendar(options, underlying)
    if max_date_dt < min_date_dt + pd.DateOffset(years=2) or options["date"].nunique() < 5:
        raise ValueError("Expected at least five sample dates spanning two years")
    underlying = underlying[(underlying["date"] >= min_date_dt) & (underlying["date"] <= max_date_dt)][["date", "close"]]
    underlying["date"] = underlying["date"].dt.strftime("%Y-%m-%d")
    rates_path = RAW / "shibor_all.csv"
    rates = load_or_fetch(rates_path, ak.macro_china_shibor_all)
    rates["日期"] = pd.to_datetime(rates["日期"], errors="coerce")
    for tenor, days in SHIBOR_TENORS.items():
        rates[f"shibor_{days}d_percent"] = pd.to_numeric(rates[f"{tenor}-定价"], errors="coerce")
    curve_columns = [f"shibor_{days}d_percent" for days in SHIBOR_TENORS.values()]
    rates = rates.dropna(subset=["日期", *curve_columns]).sort_values("日期")
    rates = pd.concat([rates[rates["日期"] < min_date_dt].tail(1), rates[rates["日期"].between(min_date_dt, max_date_dt)]])
    if rates.empty or rates["日期"].iloc[0] > min_date_dt:
        raise ValueError("No complete SHIBOR curve is available at the first quote date")
    rates = rates[["日期", *curve_columns]].rename(columns={"日期": "date"})
    rates["date"] = rates["date"].dt.strftime("%Y-%m-%d")
    write_snapshot_csv(options, options_path)
    write_snapshot_csv(underlying, CLEAN / "underlying.csv")
    write_snapshot_csv(rates, CLEAN / "rates.csv")
    metadata = {
        "source": {
            "options": "ak.option_cffex_hs300_spot_sina + ak.option_cffex_hs300_daily_sina",
            "underlying": "ak.stock_zh_index_daily(symbol='sh000300')",
            "rates": "ak.macro_china_shibor_all",
        },
        "retrieved_at": datetime.now().astimezone().isoformat(),
        "contracts": CONTRACTS,
        "sample_rule": "Full shared calendar month for four expiries, plus first-month trading dates for other sampled expiries. Selected strikes use both call and put daily closes.",
        **calendar_details,
        "shibor_tenor_days": SHIBOR_TENORS,
        "date_range": [min_date, max_date],
        "expiry_count": int(options["expiry"].nunique()),
        "sample_date_count": int(options["date"].nunique()),
        "quote_rows": int(len(options)),
        "artifacts": {},
    }
    write_metadata(metadata)
    print(json.dumps({"quote_rows": len(options), "expiry_count": options["expiry"].nunique(), "sample_date_count": options["date"].nunique(), "date_range": [min_date, max_date]}, indent=2))


if __name__ == "__main__":
    main()
