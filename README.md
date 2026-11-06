# CSI 300 Index Option Volatility Surface
Builds implied-volatility surfaces from CFFEX CSI 300 index option quotes.
Approach 1 implements Brent IV inversion, SVI, parity, repricing, and no-arbitrage checks.
Approach 2 implements differentiable SVI and a price-space neural surface in PyTorch.
Requires Python 3.13/3.14 and `uv`; stochastic runs default to seed 42.
Run: `uv sync --locked --python 3.13 && uv run --locked python data/fetch.py --seed 42 && uv run --locked python code/approach1_market_pipeline.py --seed 42 && uv run --locked python code/approach2_differentiable.py --seed 42 && uv run --locked python code/compare.py`.
