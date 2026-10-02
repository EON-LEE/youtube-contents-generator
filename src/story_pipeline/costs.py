from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import PipelineError, ceiling_micros, usd


@dataclass(frozen=True)
class Price:
    name: str
    usd_per_unit: str
    units_per_price: int
    evidence: str
    source: str

    def calculate(self, quantity: int) -> int:
        if type(quantity) is not int or quantity < 0:
            raise PipelineError("Usage must be a nonnegative integer.")
        if type(self.units_per_price) is not int or self.units_per_price <= 0:
            raise PipelineError(f"Invalid pricing unit for {self.name}.")
        try:
            rate = Decimal(self.usd_per_unit)
        except InvalidOperation:
            raise PipelineError(f"Invalid price for {self.name}.") from None
        if not rate.is_finite() or rate < 0:
            raise PipelineError(f"Invalid price for {self.name}.")
        if self.evidence not in ("public_retail", "hypothetical"):
            raise PipelineError(f"Unverified price classification for {self.name}.")
        return ceiling_micros(rate * quantity / self.units_per_price)


DEFAULT_PRICES = {
    "model_input_tokens": Price(
        "Unselected model input", "1", 1_000_000, "hypothetical", "simulation assumption"
    ),
    "model_output_tokens": Price(
        "Unselected model output", "2", 1_000_000, "hypothetical", "simulation assumption"
    ),
    "speech_characters": Price(
        "Azure S1 Neural TTS, eastus, USD, Consumption",
        "15",
        1_000_000,
        "public_retail",
        "https://prices.azure.com/api/retail/prices "
        "(observed 2026-09-20; meter 0f98e708-a16c-407b-8089-a0ed9e14ab49)",
    ),
    "images": Price("Unselected image model", "0.05", 1, "hypothetical", "simulation assumption"),
}


def estimate(usage: dict[str, int], prices: dict[str, Price]) -> dict[str, Any]:
    items = []
    total = 0
    for key, quantity in usage.items():
        if key not in prices:
            raise PipelineError(f"Missing price for {key}; execution blocked.")
        price = prices[key]
        amount = price.calculate(quantity)
        total += amount
        items.append({
            "usage": key,
            "quantity": quantity,
            "usd": usd(amount),
            "price_per_unit_usd": price.usd_per_unit,
            "units_per_price": price.units_per_price,
            "evidence": price.evidence,
            "source": price.source,
        })
    return {
        "currency": "USD",
        "total_micros": total,
        "total_usd": usd(total),
        "items": items,
        "limitations": "Fixture quantities only; no real usage, FX conversion, tax or full-video quote.",
    }


def profit_sensitivity(monthly_cash_krw: int, target_krw: int = 1_000_000) -> dict[str, Any]:
    if any(type(v) is not int or v < 0 for v in (monthly_cash_krw, target_krw)):
        raise PipelineError("Monthly cash costs and target must be nonnegative KRW integers.")
    revenue = monthly_cash_krw + target_krw
    return {
        "owner_labor_deducted": False,
        "monthly_cash_cost_krw_assumption": monthly_cash_krw,
        "target_pretax_cash_profit_krw": target_krw,
        "required_ad_revenue_krw": revenue,
        "pre_ypp_creator_ad_revenue_krw": 0,
        "scenarios": [
            {
                "hypothetical_creator_ad_krw_per_1000_total_views": rate,
                "required_monthly_catalog_views": (revenue * 1000 + rate - 1) // rate,
            }
            for rate in (1000, 3000, 5000)
        ],
        "limitations": "Not observed genre RPM. Post-platform, ad-only; not a forecast or lifetime views.",
    }
