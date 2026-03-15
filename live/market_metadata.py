"""Shared Polymarket market metadata for trading and resolution flows."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutcomeTokenMetadata:
    condition_id: str
    token_id: str
    outcome_side: str
    outcome_label: str | None
    window_slug: str
    window_end_ns: int

    @property
    def instrument_id(self) -> str:
        return f"{self.condition_id}-{self.token_id}.POLYMARKET"


@dataclass(frozen=True)
class ResolvedWindowMetadata:
    slug: str
    condition_id: str
    window_end_ns: int
    yes_token_id: str
    no_token_id: str
    yes_outcome_label: str | None
    no_outcome_label: str | None
    selected_outcome_side: str

    @property
    def selected_token_id(self) -> str:
        return self.token(self.selected_outcome_side).token_id

    @property
    def selected_outcome_label(self) -> str | None:
        return self.token(self.selected_outcome_side).outcome_label

    @property
    def instrument_id(self) -> str:
        return self.token(self.selected_outcome_side).instrument_id

    def token(self, outcome_side: str) -> OutcomeTokenMetadata:
        if outcome_side == "yes":
            return OutcomeTokenMetadata(
                condition_id=self.condition_id,
                token_id=self.yes_token_id,
                outcome_side="yes",
                outcome_label=self.yes_outcome_label,
                window_slug=self.slug,
                window_end_ns=self.window_end_ns,
            )
        if outcome_side == "no":
            return OutcomeTokenMetadata(
                condition_id=self.condition_id,
                token_id=self.no_token_id,
                outcome_side="no",
                outcome_label=self.no_outcome_label,
                window_slug=self.slug,
                window_end_ns=self.window_end_ns,
            )
        raise ValueError("outcome_side must be one of: yes, no")

    def all_tokens(self) -> tuple[OutcomeTokenMetadata, OutcomeTokenMetadata]:
        return (self.token("yes"), self.token("no"))


class WindowMetadataRegistry:
    """Lookup helper for the allowlisted market universe."""

    def __init__(self, windows: list[ResolvedWindowMetadata]):
        self._windows = tuple(windows)
        self._tokens_by_id: dict[str, OutcomeTokenMetadata] = {}
        self._windows_by_condition_id: dict[str, ResolvedWindowMetadata] = {}
        self._tokens_by_instrument_id: dict[str, OutcomeTokenMetadata] = {}

        for window in self._windows:
            self._windows_by_condition_id[window.condition_id] = window
            for token in window.all_tokens():
                self._tokens_by_id[token.token_id] = token
                self._tokens_by_instrument_id[token.instrument_id] = token

    @property
    def windows(self) -> tuple[ResolvedWindowMetadata, ...]:
        return self._windows

    def allowed_condition_ids(self) -> frozenset[str]:
        return frozenset(self._windows_by_condition_id)

    def allowed_token_ids(self) -> frozenset[str]:
        return frozenset(self._tokens_by_id)

    def token(self, token_id: str) -> OutcomeTokenMetadata | None:
        return self._tokens_by_id.get(str(token_id))

    def token_for_instrument(self, instrument_id: str) -> OutcomeTokenMetadata | None:
        return self._tokens_by_instrument_id.get(str(instrument_id))

    def window(self, condition_id: str) -> ResolvedWindowMetadata | None:
        return self._windows_by_condition_id.get(condition_id)

    def contains(self, *, condition_id: str, token_id: str) -> bool:
        token = self._tokens_by_id.get(str(token_id))
        return token is not None and token.condition_id == condition_id

