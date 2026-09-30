"""Budgets and cost (spec §6.8)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from floxim.adapters import Usage

DEFAULT_RUN_USD = 25.0
DEFAULT_RUN_TOKENS = 10_000_000


@dataclass
class Spend:
    usd: float = 0.0
    tokens: int = 0
    usd_known: bool = True  # false once any spend had no known price

    def add(self, usd: float | None, tokens: int) -> None:
        self.tokens += tokens
        if usd is None:
            self.usd_known = False
        else:
            self.usd += usd


def limit(value: Any, default: float | int | None) -> float | None:
    """A configured budget: `none` disables it; a missing value takes the default."""
    if value == "none":
        return None
    if value is None:
        return None if default is None else float(default)
    return float(value)


def run_limits(limits: dict[str, Any]) -> tuple[float | None, float | None]:
    budget = limits.get("budget") or {}
    return limit(budget.get("usd"), DEFAULT_RUN_USD), limit(
        budget.get("tokens"), DEFAULT_RUN_TOKENS
    )


def node_limits(config: dict[str, Any]) -> tuple[float | None, float | None]:
    budget = config.get("budget") or {}
    return limit(budget.get("usd"), None), limit(budget.get("tokens"), None)


def price_cost(
    prices: dict[str, Any], harness: str, model: str | None, usage: Usage
) -> float | None:
    """USD from the configured price table, or None when no price applies."""
    table = prices.get(harness) or {}
    price = table.get(model) if model else None
    if price is None and len(table) == 1:
        price = next(iter(table.values()))
    if price is None:
        return None
    cached_rate = price.get("cached_input_per_mtok", price["input_per_mtok"])
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return (
        float(
            uncached * price["input_per_mtok"]
            + usage.cached_input_tokens * cached_rate
            + usage.output_tokens * price["output_per_mtok"]
        )
        / 1_000_000
    )
