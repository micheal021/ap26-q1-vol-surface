from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import torch
from rich.console import Console
from rich.table import Table

from common import (
    ROOT,
    add_implied_volatility,
    filter_quotes,
    fit_svi,
    black_scholes_forward_price,
    butterfly_g,
    scipy_svi,
    svi_initial,
    load_market_data,
    save_plot,
    set_seed,
    svi_value,
    write_metrics,
)


def inverse_softplus(value: float) -> float:
    return float(np.log(np.expm1(max(value, 1e-6))))


def torch_svi_value(k: torch.Tensor, raw: torch.Tensor) -> torch.Tensor:
    a = torch.nn.functional.softplus(raw[0])
    b = torch.nn.functional.softplus(raw[1])
    rho = 0.999 * torch.tanh(raw[2])
    m = raw[3]
    eta = torch.nn.functional.softplus(raw[4])
    return a + b * (rho * (k - m) + torch.sqrt((k - m) ** 2 + eta**2))


def differentiable_svi(k: np.ndarray, total_variance: np.ndarray, initial: np.ndarray, epochs: int, seed: int) -> dict:
    torch.manual_seed(seed)
    k_tensor = torch.tensor(k, dtype=torch.float32)
    variance_tensor = torch.tensor(total_variance, dtype=torch.float32)
    raw = torch.nn.Parameter(torch.tensor([inverse_softplus(initial[0]), inverse_softplus(initial[1]), np.arctanh(np.clip(initial[2] / 0.999, -0.99, 0.99)), initial[3], inverse_softplus(initial[4])], dtype=torch.float32))
    optimizer = torch.optim.Adam([raw], lr=0.03)
    history = []
    started = time.perf_counter()
    for _ in range(epochs):
        prediction = torch_svi_value(k_tensor, raw)
        loss = torch.mean((prediction - variance_tensor) ** 2)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        history.append(float(loss.detach()))
    with torch.no_grad():
        history.append(float(torch.mean((torch_svi_value(k_tensor, raw) - variance_tensor)**2)))
        parameters = torch.tensor([
            torch.nn.functional.softplus(raw[0]),
            torch.nn.functional.softplus(raw[1]),
            0.999 * torch.tanh(raw[2]),
            raw[3],
            torch.nn.functional.softplus(raw[4]),
        ]).cpu().numpy()
    return {"parameters": parameters, "objective": float(history[-1]), "epochs": epochs, "fit_seconds": time.perf_counter() - started, "history": history}


class NeuralSurface(torch.nn.Module):
    def __init__(self, hidden: int = 32):
        super().__init__()
        self.network = torch.nn.Sequential(
            torch.nn.Linear(2, hidden),
            torch.nn.Tanh(),
            torch.nn.Linear(hidden, hidden),
            torch.nn.Tanh(),
            torch.nn.Linear(hidden, 1),
        )

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return 0.01 + 2.0 * torch.nn.functional.softplus(self.network(inputs).squeeze(-1))


def torch_black_price(forward, strike, tau, rate, volatility, is_call):
    sqrt_tau = torch.sqrt(torch.clamp(tau, min=1e-6))
    d1 = (torch.log(forward / strike) + 0.5 * volatility**2 * tau) / (volatility * sqrt_tau)
    d2 = d1 - volatility * sqrt_tau
    discount = torch.exp(-rate * tau)
    call = discount * (forward * torch.special.ndtr(d1) - strike * torch.special.ndtr(d2))
    put = discount * (strike * torch.special.ndtr(-d2) - forward * torch.special.ndtr(-d1))
    return torch.where(is_call > 0.5, call, put)


def surface_loss(model, inputs, forward, strike, tau, rate, is_call, target, lambda_butterfly, lambda_calendar):
    volatility = model(inputs)
    price = torch_black_price(forward, strike, tau, rate, volatility, is_call)
    price_loss = torch.mean((price - target) ** 2)
    grid_k = torch.linspace(-0.4, 0.4, 41, device=inputs.device)
    grid_tau = torch.linspace(float(torch.min(tau)), float(torch.max(tau)), 12, device=inputs.device)
    mesh_k, mesh_tau = torch.meshgrid(grid_k, grid_tau, indexing="ij")
    grid = torch.stack([mesh_k.reshape(-1), mesh_tau.reshape(-1)], dim=1).requires_grad_(True)
    grid_volatility = model(grid)
    total_variance = grid_volatility**2 * grid[:, 1]
    derivatives = torch.autograd.grad(total_variance.sum(), grid, create_graph=True)[0]
    slope = derivatives[:, 0]
    time_slope = derivatives[:, 1]
    variance = torch.clamp(total_variance, min=1e-6)
    butterfly = (1 - grid[:, 0] * slope / (2 * variance)) ** 2 - slope**2 / 4 * (1 / variance + 0.25)
    butterfly_penalty = torch.mean(torch.relu(-butterfly) ** 2)
    calendar_penalty = torch.mean(torch.relu(-time_slope) ** 2)
    total = price_loss + lambda_butterfly * butterfly_penalty + lambda_calendar * calendar_penalty
    return total, {"price_loss": float(price_loss.detach()), "butterfly_penalty": float(butterfly_penalty.detach()), "calendar_penalty": float(calendar_penalty.detach())}


def train_surface(frame: pd.DataFrame, epochs: int, lambda_butterfly: float, lambda_calendar: float, seed: int, drop_mask: np.ndarray | None = None) -> dict:
    torch.manual_seed(seed)
    selected = frame if drop_mask is None else frame.loc[drop_mask].copy()
    inputs = torch.tensor(selected[["log_moneyness", "tau"]].to_numpy(), dtype=torch.float32)
    forward = torch.tensor(selected["forward"].to_numpy(), dtype=torch.float32)
    strike = torch.tensor(selected["strike"].to_numpy(), dtype=torch.float32)
    tau = torch.tensor(selected["tau"].to_numpy(), dtype=torch.float32)
    rate = torch.tensor(selected["rate"].to_numpy(), dtype=torch.float32)
    is_call = torch.tensor((selected["option_type"] == "call").astype(float).to_numpy(), dtype=torch.float32)
    target = torch.tensor(selected["close"].to_numpy(), dtype=torch.float32)
    model = NeuralSurface()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    history = []
    started = time.perf_counter()
    for _ in range(epochs):
        loss, details = surface_loss(model, inputs, forward, strike, tau, rate, is_call, target, lambda_butterfly, lambda_calendar)
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimizer.step()
        history.append({"loss": float(loss.detach()), **details})
    return {"model": model, "history": history, "fit_seconds": time.perf_counter() - started, "train_rows": len(selected)}


def model_price_errors(model, frame: pd.DataFrame) -> np.ndarray:
    with torch.no_grad():
        inputs = torch.tensor(frame[["log_moneyness", "tau"]].to_numpy(), dtype=torch.float32)
        forward = torch.tensor(frame["forward"].to_numpy(), dtype=torch.float32)
        strike = torch.tensor(frame["strike"].to_numpy(), dtype=torch.float32)
        tau = torch.tensor(frame["tau"].to_numpy(), dtype=torch.float32)
        rate = torch.tensor(frame["rate"].to_numpy(), dtype=torch.float32)
        is_call = torch.tensor((frame["option_type"] == "call").astype(float).to_numpy(), dtype=torch.float32)
        target = torch.tensor(frame["close"].to_numpy(), dtype=torch.float32)
        prediction = torch_black_price(forward, strike, tau, rate, model(inputs), is_call)
        return (torch.abs(prediction - target) / torch.clamp(target, min=1e-8)).numpy()


def neural_arbitrage_diagnostics(model, tau_low: float, tau_high: float) -> dict:
    grid_k = torch.linspace(-0.4, 0.4, 41)
    grid_tau = torch.linspace(tau_low, tau_high, 12)
    mesh_k, mesh_tau = torch.meshgrid(grid_k, grid_tau, indexing="ij")
    grid = torch.stack([mesh_k.reshape(-1), mesh_tau.reshape(-1)], dim=1).requires_grad_(True)
    volatility = model(grid)
    total_variance = volatility**2 * grid[:, 1]
    derivatives = torch.autograd.grad(total_variance.sum(), grid, create_graph=False)[0]
    slope = derivatives[:, 0]
    time_slope = derivatives[:, 1]
    variance = torch.clamp(total_variance, min=1e-6)
    butterfly = (1 - grid[:, 0] * slope / (2 * variance)) ** 2 - slope**2 / 4 * (1 / variance + 0.25)
    bad_butterfly = butterfly.detach() < -1e-8
    bad_calendar = time_slope < -1e-8
    return {
        "butterfly_min_g": float(torch.min(butterfly.detach())),
        "butterfly_violations": int(bad_butterfly.sum()),
        "calendar_violations": int(bad_calendar.sum()),
        "butterfly_locations_k_tau": grid.detach()[bad_butterfly].tolist(),
        "calendar_locations_k_tau": grid.detach()[bad_calendar].tolist(),
    }


def svi_holdout_errors(train_frame: pd.DataFrame, test_frame: pd.DataFrame) -> pd.Series:
    errors = {}
    for key, group in test_frame.groupby(["date", "expiry"]):
        train_group = train_frame[(train_frame["date"] == key[0]) & (train_frame["expiry"] == key[1])]
        if len(train_group) < 5:
            continue
        collapsed = train_group.groupby("log_moneyness", as_index=False).agg(total_variance=("total_variance", "mean"))
        if len(collapsed) < 5:
            continue
        fitted = fit_svi(collapsed["log_moneyness"].to_numpy(), collapsed["total_variance"].to_numpy())
        for index, row in group.iterrows():
            volatility = np.sqrt(max(float(svi_value(row["log_moneyness"], fitted["parameters"])), 1e-10) / row["tau"])
            price = black_scholes_forward_price(row["forward"], row["strike"], row["tau"], row["rate"], volatility, row["option_type"])
            errors[index] = abs(price - row["close"]) / max(row["close"], 1e-8)
    return pd.Series(errors, dtype=float)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "clean")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "approach2" / "differentiable")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-volume", type=float, default=40.0)
    parser.add_argument("--moneyness-low", type=float, default=0.9)
    parser.add_argument("--moneyness-high", type=float, default=1.1)
    parser.add_argument("--svi-epochs", type=int, default=300)
    parser.add_argument("--neural-epochs", type=int, default=250)
    args = parser.parse_args()
    set_seed(args.seed)
    started = time.perf_counter()
    raw = load_market_data(args.data)
    filtered = filter_quotes(raw, args.min_volume, (args.moneyness_low, args.moneyness_high))
    frame, divergence_count = add_implied_volatility(filtered)
    torch_fits = {}
    convergence = []
    diagnostic_grid = np.linspace(-0.4, 0.4, 81)
    for key, group in frame.groupby(["date", "expiry"], sort=True):
        collapsed = group.groupby("log_moneyness", as_index=False).agg(total_variance=("total_variance", "mean"))
        if len(collapsed) < 5:
            continue
        key_text = f"{key[0].date()}|{key[1].date()}"
        k = collapsed["log_moneyness"].to_numpy()
        total_variance = collapsed["total_variance"].to_numpy()
        initial = svi_initial(k, total_variance)
        started = time.perf_counter()
        scipy_fit, _, _, scipy_history = scipy_svi(k, total_variance, initial)
        scipy_seconds = time.perf_counter() - started
        torch_fit = differentiable_svi(k, total_variance, initial, args.svi_epochs, args.seed)
        target = 1e-7
        torch_fits[key_text] = {
            "date": key[0].date().isoformat(),
            "expiry": key[1].date().isoformat(),
            "tau": float(group["tau"].iloc[0]),
            "parameters": torch_fit["parameters"],
            "butterfly_locations_k": diagnostic_grid[butterfly_g(diagnostic_grid, torch_fit["parameters"]) < -1e-8].tolist(),
        }
        convergence.append({
            "group": key_text,
            "initial_parameters": initial.tolist(),
            "target_mse": target,
            "scipy_objective": float(np.mean(scipy_fit.fun**2)),
            "torch_objective": torch_fit["objective"],
            "scipy_seconds": scipy_seconds,
            "torch_seconds": torch_fit["fit_seconds"],
            "scipy_iterations": int(scipy_fit.nfev),
            "torch_epochs": torch_fit["epochs"],
            "scipy_objective_evaluations_to_target": next((i for i, value in enumerate(scipy_history, 1) if value <= target), None),
            "torch_updates_to_target": next((i for i, value in enumerate(torch_fit["history"]) if value <= target), None),
        })
    torch_calendar = []
    dates = sorted({fit["date"] for fit in torch_fits.values()})
    for day in dates:
        fits = sorted((fit for fit in torch_fits.values() if fit["date"] == day), key=lambda fit: fit["tau"])
        for near, far in zip(fits, fits[1:]):
            difference = svi_value(diagnostic_grid, far["parameters"]) - svi_value(diagnostic_grid, near["parameters"])
            violations = difference < -1e-8
            if np.any(violations):
                torch_calendar.append({
                    "date": day, "near_expiry": near["expiry"], "far_expiry": far["expiry"],
                    "log_moneyness": diagnostic_grid[violations].tolist(),
                })
    penalty_weight = 1e8
    penalty_grid = [(0.0, 0.0), (penalty_weight, 0.0), (0.0, penalty_weight), (penalty_weight, penalty_weight)]
    ablations = {}
    for lambda_butterfly, lambda_calendar in penalty_grid:
        label = f"lambda_b={lambda_butterfly:g},lambda_t={lambda_calendar:g}"
        trained = train_surface(frame, args.neural_epochs, lambda_butterfly, lambda_calendar, args.seed)
        errors = model_price_errors(trained["model"], frame)
        ablations[label] = {"fit_seconds": trained["fit_seconds"], "final_loss": trained["history"][-1], "median_relative_price_error": float(np.median(errors)), "p95_relative_price_error": float(np.percentile(errors, 95)), "arbitrage": neural_arbitrage_diagnostics(trained["model"], float(frame["tau"].min()), float(frame["tau"].max()))}
    rng = np.random.default_rng(args.seed)
    keep_mask = np.ones(len(frame), dtype=bool)
    keep_mask[rng.choice(len(frame), size=round(0.30 * len(frame)), replace=False)] = False
    train_frame = frame.loc[keep_mask].copy()
    heldout_frame = frame.loc[~keep_mask].copy()
    robustness_model = train_surface(train_frame, args.neural_epochs, penalty_weight, penalty_weight, args.seed + 1)
    svi_errors = svi_holdout_errors(train_frame, heldout_frame)
    neural_errors = model_price_errors(robustness_model["model"], heldout_frame.loc[svi_errors.index])
    metrics = {
        "filtered_quotes": len(filtered),
        "iv_quotes": len(frame),
        "iv_divergence_count": divergence_count,
        "torch_svi_groups": len(torch_fits),
        "convergence": convergence,
        "torch_svi_diagnostics": {"groups": torch_fits, "calendar_violations": torch_calendar},
        "neural_ablations": ablations,
        "dropout_30_percent": {
            "train_rows": len(train_frame),
            "heldout_rows": len(heldout_frame),
            "common_evaluation_rows": len(svi_errors),
            "neural_median_relative_error": float(np.median(neural_errors)) if len(neural_errors) else None,
            "neural_p95_relative_error": float(np.percentile(neural_errors, 95)) if len(neural_errors) else None,
            "svi_median_relative_error": float(np.median(svi_errors)) if len(svi_errors) else None,
            "svi_p95_relative_error": float(np.percentile(svi_errors, 95)) if len(svi_errors) else None,
        },
    }
    ablation_figure = go.Figure(go.Bar(x=list(ablations), y=[item["median_relative_price_error"] for item in ablations.values()]))
    ablation_figure.update_layout(title="Neural-surface price error ablation", xaxis_title="Penalty configuration", yaxis_title="Median relative error")
    save_plot(ablation_figure, args.out / "neural_ablation.html")
    convergence_figure = go.Figure()
    convergence_figure.add_trace(go.Box(y=[item["scipy_seconds"] for item in convergence], name="SciPy least_squares"))
    convergence_figure.add_trace(go.Box(y=[item["torch_seconds"] for item in convergence], name="Torch Adam"))
    convergence_figure.update_layout(title="SVI calibration wall-time comparison", yaxis_title="Seconds")
    save_plot(convergence_figure, args.out / "svi_convergence.html")
    write_metrics(args.out / "metrics.json", {"seed": args.seed, "svi_epochs": args.svi_epochs, "neural_epochs": args.neural_epochs, "lambda_grid": penalty_grid}, metrics, time.perf_counter() - started, {"torch_svi_groups": len(torch_fits), "neural_ablations": len(ablations)})
    table = Table(title="Q1 Approach 2: differentiable calibration")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Torch SVI groups", str(len(torch_fits)))
    table.add_row("Neural ablations", str(len(ablations)))
    table.add_row("30% dropout rows", str(len(heldout_frame)))
    table.add_row("Dropout neural median error", f"{metrics['dropout_30_percent']['neural_median_relative_error']:.4f}")
    table.add_row("Dropout SVI median error", f"{metrics['dropout_30_percent']['svi_median_relative_error']:.4f}" if metrics["dropout_30_percent"]["svi_median_relative_error"] is not None else "n/a")
    Console().print(table)


if __name__ == "__main__":
    main()
