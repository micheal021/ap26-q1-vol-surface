from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq, least_squares, minimize
from scipy.stats import norm


ROOT = Path(__file__).resolve().parents[1]


def set_seed(seed: int) -> np.random.Generator:
    np.random.seed(seed)
    return np.random.default_rng(seed)


def load_market_data(data_dir: Path | None = None) -> pd.DataFrame:
    data_dir = data_dir or ROOT / "data" / "clean"
    options = pd.read_csv(data_dir / "option_quotes.csv", parse_dates=["date", "expiry"])
    underlying = pd.read_csv(data_dir / "underlying.csv", parse_dates=["date"]).rename(columns={"close": "spot"})
    rates = pd.read_csv(data_dir / "rates.csv", parse_dates=["date"])
    options = options.sort_values("date")
    underlying = underlying.sort_values("date")
    rates = rates.sort_values("date")
    frame = pd.merge_asof(options, underlying, on="date", direction="backward")
    frame = pd.merge_asof(frame.sort_values("date"), rates, on="date", direction="backward")
    maturity_days = (frame["expiry"] - frame["date"]).dt.days
    frame["tau"] = maturity_days / 365.25
    curve = sorted((int(name.split("_")[1][:-1]), name) for name in rates if name.startswith("shibor_") and name.endswith("d_percent"))
    if curve:
        tenor_days, columns = zip(*curve)
        if frame[list(columns)].isna().any().any():
            raise ValueError("A quote is missing a complete contemporaneous SHIBOR curve")
        frame["rate"] = [
            np.interp(days, tenor_days, values) / 100.0
            for days, values in zip(maturity_days, frame[list(columns)].to_numpy())
        ]
    else:
        # The original committed snapshot remains readable for reproduction.
        frame["rate"] = frame["shibor_3m_percent"] / 100.0
    frame["forward"] = frame["spot"] * np.exp(frame["rate"] * frame["tau"])
    frame["discount"] = np.exp(-frame["rate"] * frame["tau"])
    frame["log_moneyness"] = np.log(frame["strike"] / frame["forward"])
    frame["relative_volume"] = frame["volume"].fillna(0)
    return frame


def filter_quotes(frame: pd.DataFrame, min_volume: float = 1.0, moneyness_band: tuple[float, float] = (0.65, 1.35)) -> pd.DataFrame:
    lower, upper = moneyness_band
    filtered = frame[
        (frame["tau"] > 5 / 365.25)
        & (frame["tau"] <= 2.0)
        & (frame["relative_volume"] >= min_volume)
        & (frame["close"] > 0)
        & (frame["spot"] > 0)
        & (frame["strike"] / frame["forward"] >= lower)
        & (frame["strike"] / frame["forward"] <= upper)
    ].copy()
    return filtered.reset_index(drop=True)


def black_scholes_forward_price(forward, strike, tau, rate, volatility, option_type: str):
    if tau <= 0 or volatility <= 0:
        intrinsic = max(forward - strike, 0.0) if option_type == "call" else max(strike - forward, 0.0)
        return float(np.exp(-rate * max(tau, 0.0)) * intrinsic)
    sqrt_tau = np.sqrt(tau)
    d1 = (np.log(forward / strike) + 0.5 * volatility**2 * tau) / (volatility * sqrt_tau)
    d2 = d1 - volatility * sqrt_tau
    discount = np.exp(-rate * tau)
    if option_type == "call":
        return float(discount * (forward * norm.cdf(d1) - strike * norm.cdf(d2)))
    return float(discount * (strike * norm.cdf(-d2) - forward * norm.cdf(-d1)))


def implied_volatility(row: pd.Series, sigma_low: float = 1e-4, sigma_high: float = 5.0) -> float | None:
    market_price = float(row["close"])
    intrinsic = black_scholes_forward_price(row["forward"], row["strike"], row["tau"], row["rate"], sigma_low, row["option_type"])
    upper_price = black_scholes_forward_price(row["forward"], row["strike"], row["tau"], row["rate"], sigma_high, row["option_type"])
    if market_price < intrinsic - 1e-8 or market_price > upper_price + 1e-8:
        return None
    function = lambda sigma: black_scholes_forward_price(row["forward"], row["strike"], row["tau"], row["rate"], sigma, row["option_type"]) - market_price
    try:
        return float(brentq(function, sigma_low, sigma_high, xtol=1e-10, rtol=1e-10, maxiter=200))
    except ValueError:
        return None


def add_implied_volatility(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    result = frame.copy()
    result["iv"] = result.apply(implied_volatility, axis=1)
    divergence_count = int(result["iv"].isna().sum())
    result = result.dropna(subset=["iv"]).copy()
    result["total_variance"] = result["iv"] ** 2 * result["tau"]
    return result, divergence_count


def parity_residuals(frame: pd.DataFrame) -> pd.DataFrame:
    pivot = frame.pivot_table(index=["date", "expiry", "strike"], columns="option_type", values="close", aggfunc="mean").reset_index()
    if "call" not in pivot or "put" not in pivot:
        return pd.DataFrame(columns=["date", "expiry", "strike", "parity_residual"])
    enriched = frame[["date", "expiry", "strike", "spot", "rate", "tau"]].drop_duplicates(["date", "expiry", "strike"])
    result = pivot.merge(enriched, on=["date", "expiry", "strike"], how="left")
    result["parity_residual"] = result["call"] - result["put"] - (result["spot"] - result["strike"] * np.exp(-result["rate"] * result["tau"]))
    return result


def svi_value(k, parameters):
    a, b, rho, m, eta = parameters
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + eta**2))


def svi_slope(k, parameters):
    _, b, rho, m, eta = parameters
    return b * (rho + (k - m) / np.sqrt((k - m) ** 2 + eta**2))


def butterfly_g(k, parameters):
    variance = np.maximum(svi_value(k, parameters), 1e-10)
    slope = svi_slope(k, parameters)
    return (1 - k * slope / (2 * variance)) ** 2 - slope**2 / 4 * (1 / variance + 0.25)


def svi_initial(k: np.ndarray, total_variance: np.ndarray) -> np.ndarray:
    return np.array([max(float(np.percentile(total_variance, 10)), 1e-5), 0.2, 0.0, float(np.median(k)), 0.1])


def scipy_svi(k: np.ndarray, total_variance: np.ndarray, initial: np.ndarray) -> tuple:
    lower = np.array([0.0, 0.0, -0.999, -3.0, 1e-4])
    upper = np.array([5.0, 10.0, 0.999, 3.0, 5.0])
    history = []

    def residual(parameters):
        values = svi_value(k, parameters) - total_variance
        history.append(float(np.mean(values**2)))
        return values

    fit = least_squares(residual, initial, bounds=(lower, upper), max_nfev=3000, xtol=1e-12, ftol=1e-12, gtol=1e-12)
    return fit, lower, upper, history


def fit_svi(k: np.ndarray, total_variance: np.ndarray, butterfly_penalty: float = 0.0) -> dict:
    k = np.asarray(k, dtype=float)
    total_variance = np.asarray(total_variance, dtype=float)
    initial = svi_initial(k, total_variance)
    started = time.perf_counter()
    penalty_grid = np.linspace(-0.4, 0.4, 81)

    def objective(parameters):
        return float(np.mean((svi_value(k, parameters) - total_variance) ** 2))

    unconstrained, lower, upper, _ = scipy_svi(k, total_variance, initial)
    constrained = minimize(objective, unconstrained.x, method="SLSQP", bounds=list(zip(lower, upper)), constraints={"type": "ineq", "fun": lambda parameters: butterfly_g(penalty_grid, parameters)}, options={"maxiter": 1000, "ftol": 1e-12})
    result_parameters = constrained.x if constrained.success else unconstrained.x
    return {
        "parameters": result_parameters,
        "objective": objective(result_parameters),
        "iterations": int(getattr(constrained, "nit", unconstrained.nfev)),
        "success": bool(constrained.success),
        "message": str(constrained.message),
        "unconstrained_objective": float(np.mean(unconstrained.fun**2)),
        "fit_seconds": time.perf_counter() - started,
    }


def group_key(row: pd.Series) -> str:
    return f"{row['date'].date()}|{row['expiry'].date()}"


def fit_all_groups(frame: pd.DataFrame) -> dict[str, dict]:
    fits: dict[str, dict] = {}
    for key, group in frame.groupby(["date", "expiry"], sort=True):
        if len(group) < 5 or group["log_moneyness"].nunique() < 5:
            continue
        collapsed = group.groupby("log_moneyness", as_index=False).agg(total_variance=("total_variance", "mean"), tau=("tau", "first"), forward=("forward", "first"), rate=("rate", "first"))
        fitted = fit_svi(collapsed["log_moneyness"].to_numpy(), collapsed["total_variance"].to_numpy())
        key_text = f"{key[0].date()}|{key[1].date()}"
        fits[key_text] = {**fitted, "date": key[0].date().isoformat(), "expiry": key[1].date().isoformat(), "tau": float(collapsed["tau"].iloc[0]), "forward": float(collapsed["forward"].iloc[0]), "rate": float(collapsed["rate"].iloc[0]), "n_quotes": int(len(group))}
    return fits


def jsonable(value):
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    return value


def write_metrics(path: Path, params: dict, metrics: dict, fit_seconds: float, history: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"params": jsonable(params), "metrics": jsonable(metrics), "fit_seconds": float(fit_seconds), "history": jsonable(history)}, ensure_ascii=False, indent=2))


def save_plot(figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.write_html(str(path), include_plotlyjs=True)
