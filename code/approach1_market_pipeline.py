from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from rich.console import Console
from rich.table import Table

from common import (
    ROOT,
    add_implied_volatility,
    black_scholes_forward_price,
    butterfly_g,
    filter_quotes,
    fit_all_groups,
    jsonable,
    load_market_data,
    parity_residuals,
    save_plot,
    set_seed,
    svi_value,
    write_metrics,
)


def fit_diagnostics(frame, fits, grid: np.ndarray) -> tuple[dict, dict]:
    per_group = {}
    surface_rows = []
    date_max_error = {}
    for key, fitted in fits.items():
        date_text, expiry_text = key.split("|")
        group = frame[(frame["date"].dt.date.astype(str) == date_text) & (frame["expiry"].dt.date.astype(str) == expiry_text)]
        parameters = np.asarray(fitted["parameters"], dtype=float)
        butterfly_values = butterfly_g(grid, parameters)
        repricing = []
        for _, row in group.iterrows():
            variance = max(float(svi_value(row["log_moneyness"], parameters)), 1e-10)
            fitted_volatility = np.sqrt(variance / row["tau"])
            fitted_price = black_scholes_forward_price(row["forward"], row["strike"], row["tau"], row["rate"], fitted_volatility, row["option_type"])
            repricing.append(abs(fitted_price - row["close"]) / max(abs(row["close"]), 1e-8))
        if repricing:
            date_max_error[date_text] = max(date_max_error.get(date_text, 0.0), max(repricing))
        per_group[key] = {
            "date": date_text,
            "expiry": expiry_text,
            "tau": fitted["tau"],
            "n_quotes": fitted["n_quotes"],
            "parameters": parameters,
            "objective": fitted["objective"],
            "iterations": fitted["iterations"],
            "constraint_success": fitted["success"],
            "solver_message": fitted["message"],
            "fit_seconds": fitted["fit_seconds"],
            "butterfly_min_g": float(np.min(butterfly_values)),
            "butterfly_violations": int(np.sum(butterfly_values < -1e-8)),
            "butterfly_violation_k": grid[butterfly_values < -1e-8].tolist(),
            "repricing_median_relative_error": float(np.median(repricing)) if repricing else None,
            "repricing_p95_relative_error": float(np.percentile(repricing, 95)) if repricing else None,
            "repricing_max_relative_error": float(np.max(repricing)) if repricing else None,
        }
        if not surface_rows:
            surface_rows = [{"k": float(value), "fitted_iv": float(np.sqrt(max(svi_value(value, parameters), 1e-10) / fitted["tau"]))} for value in grid]
            for _, row in group.iterrows():
                surface_rows.append({"k": float(row["log_moneyness"]), "market_iv": float(row["iv"])})
    calendar_violations = 0
    negative_forward_variance = 0
    calendar_details = {}
    for date_text in sorted({key.split("|")[0] for key in fits}):
        date_fits = sorted((item for key, item in fits.items() if key.startswith(f"{date_text}|")), key=lambda item: item["tau"])
        if len(date_fits) < 2:
            continue
        previous_grid = None
        date_calendar = 0
        date_forward = 0
        locations = []
        for item in date_fits:
            current_grid = svi_value(grid, item["parameters"])
            if previous_grid is not None:
                date_calendar += int(np.sum(current_grid < previous_grid - 1e-8))
                forward = (current_grid - previous_grid) / max(item["tau"] - previous_tau, 1e-8)
                date_forward += int(np.sum(forward < -1e-8))
                flagged = (current_grid < previous_grid - 1e-8) | (forward < -1e-8)
                if np.any(flagged):
                    locations.append({"near_expiry": previous_expiry, "far_expiry": item["expiry"], "log_moneyness": grid[flagged].tolist(), "forward_variance": forward[flagged].tolist()})
            previous_grid = current_grid
            previous_tau = item["tau"]
            previous_expiry = item["expiry"]
        calendar_violations += date_calendar
        negative_forward_variance += date_forward
        calendar_details[date_text] = {"calendar_violations": date_calendar, "negative_forward_variance": date_forward, "locations": locations}
    summary = {
        "fit_count": len(per_group),
        "constrained_fit_failures": sum(not item["constraint_success"] for item in per_group.values()),
        "butterfly_violations": int(sum(item["butterfly_violations"] for item in per_group.values())),
        "calendar_violations": calendar_violations,
        "negative_forward_variance": negative_forward_variance,
        "calendar_details": calendar_details,
        "per_date_max_relative_error": date_max_error,
        "median_date_max_relative_error": float(np.median(list(date_max_error.values()))) if date_max_error else None,
        "p95_date_max_relative_error": float(np.percentile(list(date_max_error.values()), 95)) if date_max_error else None,
        "repricing_median": float(np.median([item["repricing_median_relative_error"] for item in per_group.values() if item["repricing_median_relative_error"] is not None])) if per_group else None,
        "repricing_p95": float(np.percentile([item["repricing_p95_relative_error"] for item in per_group.values() if item["repricing_p95_relative_error"] is not None], 95)) if per_group else None,
    }
    return {"per_group": per_group, "summary": summary}, {"surface": surface_rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "clean")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "approach1" / "market")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-volume", type=float, default=40.0)
    parser.add_argument("--moneyness-low", type=float, default=0.9)
    parser.add_argument("--moneyness-high", type=float, default=1.1)
    parser.add_argument("--grid-points", type=int, default=81)
    args = parser.parse_args()
    set_seed(args.seed)
    started = time.perf_counter()
    raw = load_market_data(args.data)
    filtered = filter_quotes(raw, args.min_volume, (args.moneyness_low, args.moneyness_high))
    iv_frame, divergence_count = add_implied_volatility(filtered)
    parity = parity_residuals(filtered)
    fits = fit_all_groups(iv_frame)
    grid = np.linspace(-0.4, 0.4, args.grid_points)
    diagnostics, plot_data = fit_diagnostics(iv_frame, fits, grid)
    surface_rows = []
    for key, fitted in fits.items():
        for log_moneyness in grid:
            total_variance = max(float(svi_value(log_moneyness, fitted["parameters"])), 1e-10)
            surface_rows.append({"date": fitted["date"], "expiry": fitted["expiry"], "log_moneyness": float(log_moneyness), "total_variance": total_variance, "implied_volatility": float(np.sqrt(total_variance / fitted["tau"]))})
    args.out.mkdir(parents=True, exist_ok=True)
    parity.to_csv(args.out / "parity_residuals.csv", index=False)
    failed = filtered.loc[filtered.index.difference(iv_frame.index), ["date", "expiry", "strike", "option_type", "close", "forward", "rate"]]
    failed.to_csv(args.out / "iv_divergences.csv", index=False)
    surface = pd.DataFrame(surface_rows)
    surface["tau"] = (pd.to_datetime(surface["expiry"]) - pd.to_datetime(surface["date"])).dt.days / 365.25
    surface.to_csv(args.out / "surface_grid.csv", index=False)
    midpoints = []
    for (day, k), group in surface.groupby(["date", "log_moneyness"]):
        ordered = list(group.sort_values("tau").itertuples(index=False))
        for left, right in zip(ordered, ordered[1:]):
            tau = (left.tau + right.tau) / 2
            variance = (left.total_variance + right.total_variance) / 2
            midpoints.append({"date": day, "log_moneyness": k, "near_expiry": left.expiry, "far_expiry": right.expiry, "tau": tau, "total_variance": variance, "implied_volatility": np.sqrt(variance / tau)})
    pd.DataFrame(midpoints).to_csv(args.out / "interpolated_midpoints.csv", index=False)
    parity_values = parity["parity_residual"].dropna().to_numpy() if not parity.empty else np.array([])
    metrics = {
        "raw_quotes": int(len(raw)),
        "filtered_quotes": int(len(filtered)),
        "iv_quotes": int(len(iv_frame)),
        "iv_divergence_count": divergence_count,
        "iv_divergence_rate": float(divergence_count / max(len(filtered), 1)),
        "sample_dates": int(iv_frame["date"].dt.date.nunique()),
        "expiry_count": int(iv_frame["expiry"].dt.date.nunique()),
        "parity": {
            "matched_strike_groups": int(len(parity_values)),
            "mean_residual": float(np.mean(parity_values)) if len(parity_values) else None,
            "rmse": float(np.sqrt(np.mean(parity_values**2))) if len(parity_values) else None,
            "p95_absolute": float(np.percentile(np.abs(parity_values), 95)) if len(parity_values) else None,
        },
        "svi": diagnostics,
        "filter_rule": {"min_volume": args.min_volume, "moneyness_band": [args.moneyness_low, args.moneyness_high]},
    }
    surface_figure = go.Figure()
    surface = plot_data["surface"]
    market_points = [item for item in surface if "market_iv" in item]
    fitted_points = [item for item in surface if "fitted_iv" in item]
    surface_figure.add_trace(go.Scatter(x=[item["k"] for item in market_points], y=[item["market_iv"] for item in market_points], mode="markers", name="Market IV"))
    surface_figure.add_trace(go.Scatter(x=[item["k"] for item in fitted_points], y=[item["fitted_iv"] for item in fitted_points], mode="lines", name="SVI fitted IV"))
    surface_figure.update_layout(title="Representative implied-volatility smile", xaxis_title="Log moneyness", yaxis_title="Implied volatility")
    save_plot(surface_figure, args.out / "svi_surface.html")
    violation_figure = go.Figure(go.Bar(x=["Butterfly", "Calendar", "Negative forward variance"], y=[metrics["svi"]["summary"]["butterfly_violations"], metrics["svi"]["summary"]["calendar_violations"], metrics["svi"]["summary"]["negative_forward_variance"]]))
    violation_figure.update_layout(title="No-arbitrage diagnostics", yaxis_title="Violation count")
    save_plot(violation_figure, args.out / "arbitrage_diagnostics.html")
    write_metrics(args.out / "metrics.json", {"seed": args.seed, "data": str(args.data.relative_to(ROOT)) if args.data.is_relative_to(ROOT) else str(args.data), "grid_points": args.grid_points}, metrics, time.perf_counter() - started, {"fit_groups": len(fits)})
    table = Table(title="Q1 Approach 1: market pipeline")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Filtered quotes", str(len(filtered)))
    table.add_row("IV quotes", str(len(iv_frame)))
    table.add_row("SVI groups", str(len(fits)))
    table.add_row("IV divergences", str(divergence_count))
    table.add_row("Butterfly violations", str(metrics["svi"]["summary"]["butterfly_violations"]))
    table.add_row("Median repricing", f"{metrics['svi']['summary']['repricing_median']:.4f}" if metrics["svi"]["summary"]["repricing_median"] is not None else "n/a")
    Console().print(table)


if __name__ == "__main__":
    main()
