"""Running cost, with a per-role cap and a per-run cap.

The cap is the easy part. The hard part is that a cap is only as good as the
price table behind it, which is why `pricing.PriceTable` refuses to guess. A
cap enforced against a guessed price is worse than no cap, because it stops
runs for reasons you cannot reconstruct afterwards.

When a cap is breached the harness records the spend, the cap, and the last
stop reason it saw. That last field is what tells you whether you cut off a
model that was stuck or a model that was one tool call from the answer.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .pricing import PriceTable, Usage


class BudgetExceeded(Exception):
    """Raised when a charge takes a role or the run past its cap."""

    def __init__(
        self,
        scope: str,
        spent_usd: float,
        cap_usd: float,
        last_stop_reason: str | None = None,
    ) -> None:
        self.scope = scope
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd
        self.last_stop_reason = last_stop_reason
        super().__init__(
            f"{scope} budget exceeded: ${spent_usd:.4f} of ${cap_usd:.4f} cap "
            f"(last stop_reason={last_stop_reason!r})"
        )


@dataclasses.dataclass
class Charge:
    """One priced model response."""

    role: str
    model: str
    usage: Usage
    cost_usd: float
    cumulative_usd: float


class Budget:
    """Cost accounting for one run."""

    def __init__(
        self,
        prices: PriceTable,
        run_cap_usd: float | None = None,
        role_caps_usd: dict[str, float] | None = None,
        trace: Any = None,
    ) -> None:
        self.prices = prices
        self.run_cap_usd = run_cap_usd
        self.role_caps_usd = dict(role_caps_usd or {})
        self.trace = trace
        self.spent_usd = 0.0
        self.per_role_usd: dict[str, float] = {}
        self.per_role_usage: dict[str, Usage] = {}
        self.last_stop_reason: str | None = None

    def role_spent(self, role: str) -> float:
        return self.per_role_usd.get(role, 0.0)

    def remaining(self) -> float | None:
        if self.run_cap_usd is None:
            return None
        return max(0.0, self.run_cap_usd - self.spent_usd)

    def charge(
        self,
        *,
        role: str,
        model: str,
        usage: Usage,
        stop_reason: str | None = None,
    ) -> Charge:
        """Price one response and add it to the totals. Does not raise.

        Charging and enforcing are separate calls so the caller can write the
        `model_response` trace event for the response that broke the cap before
        the exception unwinds. A breach you cannot see the cost of is a breach
        you will argue about later.
        """
        cost = self.prices.cost(model, usage)
        self.spent_usd += cost
        self.per_role_usd[role] = self.per_role_usd.get(role, 0.0) + cost
        self.per_role_usage[role] = self.per_role_usage.get(role, Usage()) + usage
        if stop_reason is not None:
            self.last_stop_reason = stop_reason

        return Charge(
            role=role,
            model=model,
            usage=usage,
            cost_usd=cost,
            cumulative_usd=self.spent_usd,
        )

    def enforce(self, role: str) -> None:
        """Raise BudgetExceeded if `role` or the run is over its cap."""
        role_cap = self.role_caps_usd.get(role)
        if role_cap is not None and self.role_spent(role) > role_cap:
            self._record(f"role:{role}", self.role_spent(role), role_cap, breached=True)
            raise BudgetExceeded(
                f"role:{role}", self.role_spent(role), role_cap, self.last_stop_reason
            )
        if self.run_cap_usd is not None and self.spent_usd > self.run_cap_usd:
            self._record("run", self.spent_usd, self.run_cap_usd, breached=True)
            raise BudgetExceeded(
                "run", self.spent_usd, self.run_cap_usd, self.last_stop_reason
            )

    def _record(self, scope: str, spent: float, cap: float | None, breached: bool) -> None:
        if self.trace is None:
            return
        self.trace.budget(
            scope=scope,
            spent_usd=spent,
            cap_usd=cap,
            breached=breached,
            last_stop_reason=self.last_stop_reason,
        )

    def snapshot(self, scope: str = "run") -> None:
        """Write a non-breaching `budget` event, for the end of a stage."""
        cap = self.run_cap_usd if scope == "run" else self.role_caps_usd.get(scope)
        spent = self.spent_usd if scope == "run" else self.role_spent(scope)
        self._record(scope, spent, cap, breached=False)
