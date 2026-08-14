"""
Smart Fitness Tracker — Progression Algorithm
================================================
Design pattern: STRATEGY

Why Strategy here specifically: the *shape* of the progression decision
(progressive overload rule, deload rule) is identical for every exercise —
but *how weight can physically be changed* differs completely by equipment:

  - Free weights: increment_step usually means "per dumbbell" — jumping in
    quantized units. Weight increases are always possible until a real
    physical ceiling.
  - Machines: increment_step is a pin-stack jump, often coarser.
  - Bodyweight: increment_step is often 0 (or represents an added weight
    vest). If it's 0, weight can NEVER increase — the only lever is reps
    (or eventually a harder exercise variation, which is out of scope here).

Rather than branching on `equipment_type == "..."` inside one god-function,
each equipment type gets its own Strategy class implementing a shared
interface. The ProgressionEngine (context) doesn't know or care which
concrete strategy it's holding.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Optional


# ============================================================================
# Value objects (mirror the DB rows, kept framework-agnostic)
# ============================================================================

class EquipmentType(str, Enum):
    FREE_WEIGHT = "FREE_WEIGHT"
    MACHINE = "MACHINE"
    BODYWEIGHT = "BODYWEIGHT"
    BAND = "BAND"


class Difficulty(str, Enum):
    """Derived from RPE, not stored redundantly in the DB."""
    EASY = "EASY"
    MODERATE = "MODERATE"
    HARD = "HARD"

    @classmethod
    def from_rpe(cls, rpe: float) -> "Difficulty":
        if rpe <= 6:
            return cls.EASY
        if rpe <= 8:
            return cls.MODERATE
        return cls.HARD


@dataclass(frozen=True)
class ExerciseConfig:
    """Static config for an exercise — maps 1:1 to the `exercises` table."""
    exercise_id: int
    equipment_type: EquipmentType
    increment_step: float          # S in the rounding formula
    min_reps_target: int
    max_reps_target: int
    max_weight_limit: Optional[float] = None


@dataclass
class Prescription:
    """Mutable current state — maps to a `routine_exercises` row."""
    weight: float
    reps_target: int
    sets: int
    consecutive_easy_count: int = 0


@dataclass(frozen=True)
class SessionResult:
    """Summary of the most recently logged session for one exercise."""
    session_date: date
    difficulty: Difficulty         # worst-case or average RPE bucket for the session
    hit_rep_target: bool           # did the person complete reps_target on all sets?


# ============================================================================
# STRATEGY INTERFACE
# ============================================================================

class WeightProgressionStrategy(ABC):
    """
    Each concrete strategy owns everything that depends on *how you can
    physically load this equipment*: rounding, whether weight increases
    are possible at all, and the actual arithmetic for a weight bump.
    """

    @abstractmethod
    def can_increase_weight(self, config: ExerciseConfig) -> bool:
        """False for e.g. plain bodyweight work with increment_step == 0."""
        raise NotImplementedError

    @abstractmethod
    def round_to_valid_weight(self, raw_weight: float, config: ExerciseConfig) -> float:
        """
        Applies W_final = round(W_calc / S) * S.
        Equipment-specific because some equipment (e.g. free weights loaded
        symmetrically) may need the step interpreted differently.
        """
        raise NotImplementedError

    @abstractmethod
    def bump_weight(self, current_weight: float, config: ExerciseConfig) -> float:
        """Return the next weight up, pre-rounding, for a weight-progression event."""
        raise NotImplementedError

    def clamp_to_limit(self, weight: float, config: ExerciseConfig) -> float:
        """Shared safety guard — prevents infinite progression (per the brief)."""
        if config.max_weight_limit is not None:
            return min(weight, config.max_weight_limit)
        return weight


class FreeWeightStrategy(WeightProgressionStrategy):
    """Dumbbells / barbells / kettlebells. Increment step usually 2.5–5 kg."""

    def can_increase_weight(self, config: ExerciseConfig) -> bool:
        return config.increment_step > 0

    def round_to_valid_weight(self, raw_weight: float, config: ExerciseConfig) -> float:
        step = config.increment_step
        return round(raw_weight / step) * step if step > 0 else raw_weight

    def bump_weight(self, current_weight: float, config: ExerciseConfig) -> float:
        # Smallest physical jump: one increment step.
        return current_weight + config.increment_step


class MachineStrategy(WeightProgressionStrategy):
    """Selectorized / plate-loaded machines. Usually a coarser step than free weights."""

    def can_increase_weight(self, config: ExerciseConfig) -> bool:
        return config.increment_step > 0

    def round_to_valid_weight(self, raw_weight: float, config: ExerciseConfig) -> float:
        step = config.increment_step
        return round(raw_weight / step) * step if step > 0 else raw_weight

    def bump_weight(self, current_weight: float, config: ExerciseConfig) -> float:
        return current_weight + config.increment_step


class BodyweightStrategy(WeightProgressionStrategy):
    """
    Bodyweight movements. If increment_step is 0 (pure bodyweight, no vest),
    weight progression is physically impossible — the engine must fall back
    to reps-only progression. If a vest/belt is used, increment_step > 0
    and this behaves like FreeWeightStrategy.
    """

    def can_increase_weight(self, config: ExerciseConfig) -> bool:
        return config.increment_step > 0

    def round_to_valid_weight(self, raw_weight: float, config: ExerciseConfig) -> float:
        step = config.increment_step
        if step <= 0:
            return 0.0
        return round(raw_weight / step) * step

    def bump_weight(self, current_weight: float, config: ExerciseConfig) -> float:
        if config.increment_step <= 0:
            # No physical way to add load — caller should be routing to
            # rep-progression instead; this is a defensive fallback.
            return current_weight
        return current_weight + config.increment_step


# ----------------------------------------------------------------------------
# Strategy factory — keeps the mapping in one place (Open/Closed: add a new
# equipment type by adding one class + one line here, nothing else changes).
# ----------------------------------------------------------------------------

_STRATEGY_REGISTRY: dict[EquipmentType, WeightProgressionStrategy] = {
    EquipmentType.FREE_WEIGHT: FreeWeightStrategy(),
    EquipmentType.MACHINE: MachineStrategy(),
    EquipmentType.BODYWEIGHT: BodyweightStrategy(),
}


def get_strategy(equipment_type: EquipmentType) -> WeightProgressionStrategy:
    try:
        return _STRATEGY_REGISTRY[equipment_type]
    except KeyError:
        raise ValueError(f"No progression strategy registered for {equipment_type}")


# ============================================================================
# CONTEXT — orchestrates the equipment-agnostic business rules and delegates
# the equipment-specific math to whichever strategy it's holding.
# ============================================================================

class ProgressionEngine:
    """
    Applies, in priority order:
      1. Deload check (time-based, equipment-agnostic trigger, but the
         resulting weight still has to be rounded via the strategy).
      2. Progressive overload check (2 consecutive "easy" sessions).
    """

    DELOAD_GAP_DAYS = 14
    DELOAD_PERCENTAGE = 0.10          # reduce prescribed weight by 10%
    EASY_STREAK_TO_PROGRESS = 2

    def __init__(self, strategy: WeightProgressionStrategy):
        self._strategy = strategy

    def compute_next_prescription(
        self,
        config: ExerciseConfig,
        current: Prescription,
        last_session: Optional[SessionResult],
        today: Optional[date] = None,
    ) -> Prescription:
        today = today or date.today()

        # --- Rule 1: Deload takes priority over everything else ------------
        if last_session and (today - last_session.session_date) > timedelta(days=self.DELOAD_GAP_DAYS):
            return self._apply_deload(config, current)

        # No session yet, or last session wasn't "easy" -> hold steady.
        if not last_session or last_session.difficulty != Difficulty.EASY:
            return Prescription(
                weight=current.weight,
                reps_target=current.reps_target,
                sets=current.sets,
                consecutive_easy_count=0 if not last_session or last_session.difficulty != Difficulty.EASY else current.consecutive_easy_count,
            )

        # --- Rule 2: Progressive overload -----------------------------------
        new_easy_streak = current.consecutive_easy_count + 1

        if new_easy_streak < self.EASY_STREAK_TO_PROGRESS:
            # One easy session isn't enough yet — just record the streak.
            return Prescription(
                weight=current.weight,
                reps_target=current.reps_target,
                sets=current.sets,
                consecutive_easy_count=new_easy_streak,
            )

        # Two consecutive easy sessions reached -> progress.
        return self._apply_progressive_overload(config, current)

    # ------------------------------------------------------------------------

    def _apply_progressive_overload(self, config: ExerciseConfig, current: Prescription) -> Prescription:
        # Reps first, up to the max rep threshold...
        if current.reps_target < config.max_reps_target:
            return Prescription(
                weight=current.weight,
                reps_target=current.reps_target + 1,
                sets=current.sets,
                consecutive_easy_count=0,
            )

        # ...then weight, with reps reset back to the floor.
        if self._strategy.can_increase_weight(config):
            raw_new_weight = self._strategy.bump_weight(current.weight, config)
            rounded = self._strategy.round_to_valid_weight(raw_new_weight, config)
            clamped = self._strategy.clamp_to_limit(rounded, config)
            return Prescription(
                weight=clamped,
                reps_target=config.min_reps_target,
                sets=current.sets,
                consecutive_easy_count=0,
            )

        # Bodyweight exercise with no way to add load (e.g. no vest) and
        # already at max reps: nothing more to progress automatically —
        # hold at current prescription (a real app would flag this for
        # the user to consider a harder exercise variation).
        return Prescription(
            weight=current.weight,
            reps_target=current.reps_target,
            sets=current.sets,
            consecutive_easy_count=0,
        )

    def _apply_deload(self, config: ExerciseConfig, current: Prescription) -> Prescription:
        raw_deload_weight = current.weight * (1 - self.DELOAD_PERCENTAGE)
        rounded = self._strategy.round_to_valid_weight(raw_deload_weight, config)
        return Prescription(
            weight=max(rounded, 0.0),
            reps_target=config.min_reps_target,
            sets=current.sets,
            consecutive_easy_count=0,
        )

if __name__ == "__main__":
    config = ExerciseConfig(
        exercise_id=1,
        equipment_type=EquipmentType.FREE_WEIGHT,
        increment_step=2.5,
        min_reps_target=8,
        max_reps_target=12,
        max_weight_limit=60.0,
    )
    current = Prescription(weight=20.0, reps_target=12, sets=3, consecutive_easy_count=1)
    last_session = SessionResult(session_date=date.today() - timedelta(days=3),
                                  difficulty=Difficulty.EASY,
                                  hit_rep_target=True)

    engine = ProgressionEngine(get_strategy(config.equipment_type))
    next_prescription = engine.compute_next_prescription(config, current, last_session)

    print(next_prescription)