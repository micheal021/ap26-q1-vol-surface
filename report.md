# Q1 Implied Volatility Surface of China Index Options

## 1. Executive summary

This study uses CFFEX CSI 300 index options. Approach 1 uses Brent implied-volatility inversion, SVI calibration, parity checks, repricing and arbitrage diagnostics. Approach 2 compares SciPy and Torch SVI and fits a neural surface in option-price space. The expanded snapshot produces 86 SVI fits, 0 butterfly grid violations and 3.08% median group repricing error. The classical repricing error exceeds the assignment's indicative 2% benchmark.

## 2. Data and provenance

The snapshot contains 5,898 quote rows, 12 expiries and 178 dates from 2023-03-20 to 2026-01-13. The one-month block starts on 2023-10-23 and ends before 2023-11-23. It contains 23 trading days and 5 common expiries with matched calls and puts on every day (2023-12-15, 2024-01-19, 2024-03-15, 2024-06-21, 2024-09-20). The exchange's historical daily XML supplies closes, volumes and expiration dates for the month. The original Sina sample supplies the additional dates outside the block. The CSI 300 close comes from the Sina index snapshot.

The committed curve uses 1-, 30-, 91-, 182- and 365-day SHIBOR from the verified Q2 snapshot, with its commit and source checksum recorded in metadata. The latest available curve on or before each option quote is linearly interpolated to the quote's maturity in days, with the nearest tenor held constant outside the curve. SHIBOR rates are used as proxies for continuously compounded zero rates. Clean artifacts, their SHA-256 checksums and the date-by-date chain coverage are recorded in `data/clean/metadata.json`.

The liquidity screen requires volume of at least 40 and forward moneyness between 0.90 and 1.10. The archived daily data have no synchronized bid and ask prices, so no spread filter is applied. The index dividend yield is assumed to be zero, which can affect parity residuals and fitted implied volatilities.

## 3. Market definitions

For quote date `t`, spot `S`, continuously compounded rate `r`, time to expiry `T`, and zero dividend yield, the forward is

\[
F=S e^{rT}, \qquad D=e^{-rT}.
\]

For volatility `sigma`,

\[
d_1=\frac{\log(F/K)+\frac12\sigma^2T}{\sigma\sqrt{T}},
\qquad d_2=d_1-\sigma\sqrt{T}.
\]

The Black–Scholes forward prices are

\[
C=D(FN(d_1)-KN(d_2)),
\qquad P=D(KN(-d_2)-FN(-d_1)).
\]

Implied volatility is obtained separately for every call and put using bracketed Brent root finding on `[10^-4, 5]`. 25 of 1496 filtered quotes have no bracketed IV solution. The failed observations are listed in `results/approach1/market/iv_divergences.csv`.

Put–call parity is evaluated as

\[
\epsilon_{PC}=C-P-(S-Ke^{-rT}).
\]

There are 166 matched strike groups after filtering. The mean residual is 0.0165 index points, RMSE is 14.4405, and the 95th percentile absolute residual is 19.0721. The large dispersion is retained as a data-quality finding because daily closes are not executable bid/ask mids.

## 4. Approach 1: classical market pipeline

For each date/expiry group, total variance is `w(k)=sigma_IV^2 T` and log-moneyness is `k=log(K/F)`. SVI is

\[
w(k)=a+b\left[\rho(k-m)+\sqrt{(k-m)^2+\eta^2}\right],
\]

with `b >= 0`, `|rho| < 1`, and `eta > 0`. The implementation first obtains a bounded least-squares initialization and then uses SLSQP with inequality constraints `g(k)>=0` on an 81-point interior grid. The objective remains squared total-variance error; constraint failure is reported.

The Gatheral butterfly diagnostic is

\[
g(k)=\left(1-\frac{kw'(k)}{2w(k)}\right)^2-
\frac{w'(k)^2}{4}\left(\frac1{w(k)}+\frac14\right).
\]

Calendar checks compare fitted total variance at the same `k` for increasing expiries. Forward variance is the finite difference of total variance divided by the maturity difference. The surface is delivered on an 81-point fixed grid in `results/approach1/market/surface_grid.csv`.

### 4.1 Results

| Quantity | Result |
|---|---:|
| Filtered quotes | 1496 |
| Successful IV quotes | 1471 |
| SVI groups | 86 |
| Constrained SVI failures (bounded least-squares fallback) | 0 |
| IV divergence cases | 25 |
| Matched parity pairs | 454 |
| Butterfly violations | 0 |
| Calendar violations | 682 |
| Negative forward-variance locations | 682 |
| Median of group-median repricing errors | 3.08% |
| Median of daily maximum repricing errors | 42.91% |
| 95th percentile of daily maximum repricing errors | 247.11% |

Calendar violations appear on 22 of the 23 dates with more than one fitted expiry. `metrics.json` locates violations by date, adjacent expiries and log moneyness. The term structure uses piecewise linear interpolation in total variance. The midpoint file records the interpolated values, including segments with negative forward variance. Very small option prices can produce large percentage errors. Parity residuals are computed per matched strike and have RMSE 10.02 index points.

[SVI smile](results/approach1/market/svi_surface.html) · [Arbitrage diagnostics](results/approach1/market/arbitrage_diagnostics.html)

## 5. Approach 2: differentiable calibration

### 5.1 Differentiable SVI

The PyTorch SVI implementation uses reparameterization:

\[
a=\operatorname{softplus}(u_a),\quad b=\operatorname{softplus}(u_b),\quad
\rho=0.999\tanh(u_\rho),\quad \eta=\operatorname{softplus}(u_\eta).
\]

Both optimizers start from the same parameters and are compared against the same total-variance MSE target of 1e-7. SciPy uses bounded least squares and Torch runs 300 Adam updates. SciPy reached the target in 72 of 86 groups and Torch in 14 of 86. The median final objective is 1.52e-08 for SciPy and 2.63e-07 for Torch. Median wall times are 0.021 s and 0.160 s respectively. The metrics record SciPy function evaluations and Adam updates separately, as the two counts measure different operations. The classical pipeline's constrained SLSQP stage is outside this optimizer comparison.

### 5.2 Neural surface

The neural surface is an MLP with two hidden tanh layers and a positive softplus volatility output. Inputs are `(k,T)`. The loss is option-price squared error plus arbitrage penalties:

\[
\mathcal L=\operatorname{MSE}(\hat C-C_{mkt})
 +\lambda_b\operatorname{mean}[\max(0,-g(k))^2]
 +\lambda_T\operatorname{mean}[\max(0,-\partial_Tw)^2].
\]

The four runs switch each penalty independently between 0 and 1e8. The nonzero weights put variance diagnostics on a usable scale alongside squared index-price errors.

| Penalty configuration | Median in-sample relative error | Butterfly violations | Calendar violations |
|---|---:|---:|---:|
| lambda_b=0,lambda_t=0 | 9.08% | 0 | 13 |
| lambda_b=1e+08,lambda_t=0 | 9.08% | 0 | 13 |
| lambda_b=0,lambda_t=1e+08 | 8.85% | 0 | 2 |
| lambda_b=1e+08,lambda_t=1e+08 | 8.85% | 0 | 2 |

### 5.3 Robustness

Seed 42 holds out 441 of 1471 quotes, leaving 1030 for training. Both models are compared on the same 349 held-out rows for which a local SVI refit has enough distinct training strikes. Absolute errors are divided by the observed option price.

| Model | Median relative error | P95 relative error |
|---|---:|---:|
| SVI refit | 5.08% | 74.39% |
| Neural surface | 9.25% | 71.83% |

[Calibration comparison](results/approach2/differentiable/svi_convergence.html) · [Neural ablation](results/approach2/differentiable/neural_ablation.html)

## 6. Reproducibility and validation

The environment is Python 3.13 with dependencies pinned in `pyproject.toml` and `uv.lock`. The default seed is 42. The canonical commands are:

```bash
uv sync --locked --python 3.13
uv run --locked python data/fetch.py --seed 42
uv run --locked python code/approach1_market_pipeline.py --seed 42
uv run --locked python code/approach2_differentiable.py --seed 42
uv run --locked python code/compare.py
```

Both approaches expose `argparse` CLIs, write the required `metrics.json` schema, use self-contained Plotly HTML, and preserve quote filtering, divergence cases, parity residuals, no-arbitrage counts, convergence information, and dropout results.

## 7. Limitations

Daily closes may be stale and asynchronous. The zero-dividend assumption and SHIBOR proxy also affect the forward and the repricing tail. The raw calendar meets the maturity coverage check, but liquidity filtering can leave too few strikes for individual SVI fits. Violations in the fitted surface are numerical diagnostics under these assumptions. SVI and the neural model should be compared with executable bid/ask data before making market-arbitrage claims.

## 8. Division of work

The terminal workflow implemented the data retrieval, checksum metadata, numerical estimators, constrained calibration, neural calibration, robustness experiments, figures, and reproducibility scaffolding. The group is responsible for reviewing assumptions, checking the mathematics, approving the quote filter and dividend convention, and making final claims.
