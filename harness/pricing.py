"""Price lookup. Every dollar figure the harness reports comes from here.

Three rules, and all three exist because of a specific failure:

* Gateway prefixes are stripped before lookup, so `or/vendor/model` and
  `vendor/model` price identically.
* A local prefix means $0.00. Your GPU is already paid for.
* A model that is neither listed nor local is an error at startup, not a
  guess. A guessed price is a budget guard that kills runs for no reason.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Iterable, Mapping

import yaml

DEFAULT_PRICES_PATH = Path("config/prices.yaml")


class UnknownModelError(Exception):
    """Raised when a model is not in the price table and is not local."""


@dataclasses.dataclass(frozen=True)
class Usage:
    """Token counts for one model response, as reported by the provider.

    `input_tokens` is the whole prompt, the way provider dashboards report it,
    and `cache_read_tokens` is the part of that prompt which was served from
    cache. They overlap on purpose, so a trace line can be compared to a
    provider invoice without arithmetic. `cost()` does the subtraction.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Price:
    """USD per 1M tokens."""

    input: float
    output: float
    cache_read: float = 0.0

    @property
    def is_free(self) -> bool:
        return self.input == 0.0 and self.output == 0.0 and self.cache_read == 0.0


FREE = Price(input=0.0, output=0.0, cache_read=0.0)

PER_MILLION = 1_000_000


@dataclasses.dataclass(frozen=True)
class Resolution:
    """What the table decided about one model name, and why."""

    model: str          # the name as written in roles.yaml
    lookup_key: str     # the name actually looked up, after prefix stripping
    prefix: str | None  # the prefix that was stripped, if any
    price: Price
    local: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "lookup_key": self.lookup_key,
            "prefix": self.prefix,
            "local": self.local,
            "input_per_mtok": self.price.input,
            "output_per_mtok": self.price.output,
            "cache_read_per_mtok": self.price.cache_read,
        }


class PriceTable:
    """The price table for one run. Loaded once, then read-only."""

    def __init__(
        self,
        models: Mapping[str, Mapping[str, float]] | None = None,
        gateway_prefixes: Iterable[str] = (),
        local_prefixes: Iterable[str] = (),
        source: str = "<inline>",
        verified: str | None = None,
    ) -> None:
        self.source = source
        self.verified = verified
        self.gateway_prefixes = tuple(gateway_prefixes)
        self.local_prefixes = tuple(local_prefixes)
        self._models: dict[str, Price] = {}
        for name, row in (models or {}).items():
            self._models[name] = Price(
                input=float(row["input"]),
                output=float(row["output"]),
                cache_read=float(row.get("cache_read", 0.0)),
            )

    # -- loading -------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PRICES_PATH) -> "PriceTable":
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            models=data.get("models") or {},
            gateway_prefixes=data.get("gateway_prefixes") or (),
            local_prefixes=data.get("local_prefixes") or (),
            source=str(path),
            verified=str(data.get("prices_verified")) if data.get("prices_verified") else None,
        )

    # -- name handling -------------------------------------------------------

    def split_prefix(self, model: str) -> tuple[str | None, str]:
        """Return (prefix, rest) for one leading gateway or local prefix.

        Only the first segment is considered, and only if it is a prefix we
        were told about. `anthropic/claude-sonnet-5.5` keeps its vendor
        segment, because `anthropic` is not a gateway.
        """
        if "/" not in model:
            return None, model
        head, rest = model.split("/", 1)
        if head in self.gateway_prefixes or head in self.local_prefixes:
            return head, rest
        return None, model

    def strip_prefix(self, model: str) -> str:
        """The name used for lookup, with a known gateway prefix removed."""
        prefix, rest = self.split_prefix(model)
        if prefix is not None and prefix in self.gateway_prefixes:
            return rest
        return model

    def is_local(self, model: str) -> bool:
        prefix, _ = self.split_prefix(model)
        return prefix is not None and prefix in self.local_prefixes

    # -- pricing -------------------------------------------------------------

    def resolve(self, model: str) -> Resolution:
        """Resolve one model name to a price, or raise UnknownModelError."""
        if self.is_local(model):
            prefix, rest = self.split_prefix(model)
            return Resolution(
                model=model, lookup_key=rest, prefix=prefix, price=FREE, local=True
            )

        key = self.strip_prefix(model)
        prefix, _ = self.split_prefix(model)
        price = self._models.get(key)
        if price is None:
            raise UnknownModelError(
                f"model {model!r} is not priced. Looked up {key!r} in {self.source}.\n"
                f"Add it under `models:` in {self.source}, or route it through a local "
                f"prefix ({', '.join(self.local_prefixes) or 'none configured'}) if it "
                f"runs on your own hardware. The harness will not guess a price."
            )
        return Resolution(
            model=model, lookup_key=key, prefix=prefix, price=price, local=False
        )

    def validate(self, models: Iterable[str]) -> list[Resolution]:
        """Resolve every model a run will use. Call this before spending anything.

        Raises UnknownModelError on the first unpriced model, with every
        unpriced name in the message so one run start fixes the whole table.
        """
        resolved: list[Resolution] = []
        missing: list[str] = []
        for model in dict.fromkeys(models):  # de-duplicate, keep order
            try:
                resolved.append(self.resolve(model))
            except UnknownModelError:
                missing.append(model)
        if missing:
            names = ", ".join(repr(m) for m in missing)
            raise UnknownModelError(
                f"{len(missing)} model(s) are not priced: {names}.\n"
                f"Add them under `models:` in {self.source} (USD per 1M tokens), or "
                f"give them a local prefix if they run on your own hardware."
            )
        return resolved

    def cost(self, model: str, usage: Usage) -> float:
        """Cost in USD for one response.

        Cached prompt tokens are billed at the cache-read rate and not also at
        the full input rate. Reasoning tokens are already inside the provider's
        output count, so they are not charged twice here.
        """
        price = self.resolve(model).price
        if price.is_free:
            return 0.0
        cached = min(usage.cache_read_tokens, usage.input_tokens)
        fresh_input = usage.input_tokens - cached
        return (
            fresh_input * price.input
            + cached * price.cache_read
            + usage.output_tokens * price.output
        ) / PER_MILLION
