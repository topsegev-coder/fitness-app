"""
Smart Fitness Tracker — Progression Algorithm
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Optional

class EquipmentType(str, Enum):
    FREE_WEIGHT = "FREE_WEIGHT"
    MACHINE = "MACHINE"
    BODYWEIGHT = "BODYWEIGHT"
    BAND = "BAND"

class Difficulty(str, Enum):
    EASY = "EASY"
    MODERATE = "MODERATE"
    HARD = "HARD"

    @classmethod
    def from_rpe(cls, rpe: float) -> "Difficulty":
        if rpe <= 6: return cls.EASY
        if rpe <= 8: return cls.MODERATE
        return cls.HARD

@dataclass(frozen=True)
class ExerciseConfig:
    exercise_id: int
    equipment_type: EquipmentType
    increment_step: float
    target_type: str       # 'reps' or 'time'
    initial_weight: float
    initial_target: int

@dataclass
class Prescription:
    weight: float
    reps_target: int
    sets: int
    consecutive_easy_count: int = 0

@dataclass(frozen=True)
class SessionResult:
    session_date: date
    difficulty: Difficulty
    hit_rep_target: bool

class WeightProgressionStrategy(ABC):
    @abstractmethod
    def can_increase_weight(self, config: ExerciseConfig) -> bool:
        raise NotImplementedError

    @abstractmethod
    def round_to_valid_weight(self, raw_weight: float, config: ExerciseConfig) -> float:
        raise NotImplementedError

    @abstractmethod
    def bump_weight(self, current_weight: float, config: ExerciseConfig) -> float:
        raise NotImplementedError

class FreeWeightStrategy(WeightProgressionStrategy):
    def can_increase_weight(self, config: ExerciseConfig) -> bool:
        return config.increment_step > 0

    def round_to_valid_weight(self, raw_weight: float, config: ExerciseConfig) -> float:
        step = config.increment_step
        return round(raw_weight / step) * step if step > 0 else raw_weight

    def bump_weight(self, current_weight: float, config: ExerciseConfig) -> float:
        return current_weight + config.increment_step

class MachineStrategy(WeightProgressionStrategy):
    def can_increase_weight(self, config: ExerciseConfig) -> bool:
        return config.increment_step > 0

    def round_to_valid_weight(self, raw_weight: float, config: ExerciseConfig) -> float:
        step = config.increment_step
        return round(raw_weight / step) * step if step > 0 else raw_weight

    def bump_weight(self, current_weight: float, config: ExerciseConfig) -> float:
        return current_weight + config.increment_step

class BodyweightStrategy(WeightProgressionStrategy):
    def can_increase_weight(self, config: ExerciseConfig) -> bool:
        return config.increment_step > 0

    def round_to_valid_weight(self, raw_weight: float, config: ExerciseConfig) -> float:
        step = config.increment_step
        return round(raw_weight / step) * step if step > 0 else 0.0

    def bump_weight(self, current_weight: float, config: ExerciseConfig) -> float:
        return current_weight + config.increment_step if config.increment_step > 0 else current_weight

_STRATEGY_REGISTRY: dict[EquipmentType, WeightProgressionStrategy] = {
    EquipmentType.FREE_WEIGHT: FreeWeightStrategy(),
    EquipmentType.MACHINE: MachineStrategy(),
    EquipmentType.BODYWEIGHT: BodyweightStrategy(),
}

def get_strategy(equipment_type: EquipmentType) -> WeightProgressionStrategy:
    return _STRATEGY_REGISTRY[equipment_type]

class ProgressionEngine:
    def __init__(self, strategy: WeightProgressionStrategy):
        self._strategy = strategy

    def compute_next_prescription(
        self, config: ExerciseConfig, current: Prescription, last_session: Optional[SessionResult], today: Optional[date] = None
    ) -> Prescription:
        today = today or date.today()

        # 1. Deload Rules
        if last_session:
            days_since = (today - last_session.session_date).days
            if days_since >= 30:
                return self._apply_deload(config, current, severity=2)
            elif days_since >= 14:
                return self._apply_deload(config, current, severity=1)

        if not last_session or last_session.difficulty != Difficulty.EASY:
            return Prescription(weight=current.weight, reps_target=current.reps_target, sets=current.sets, consecutive_easy_count=0 if not last_session or last_session.difficulty != Difficulty.EASY else current.consecutive_easy_count)

        # 2. Progressive Overload Trigger
        new_easy_streak = current.consecutive_easy_count + 1
        if new_easy_streak < 2:
            return Prescription(weight=current.weight, reps_target=current.reps_target, sets=current.sets, consecutive_easy_count=new_easy_streak)

        return self._apply_progressive_overload(config, current)

    def _apply_progressive_overload(self, config: ExerciseConfig, current: Prescription) -> Prescription:
        is_time = (config.target_type == "time")
        target_jump = 15 if is_time else 2
        max_target = (config.initial_target * 2) if is_time else (config.initial_target + 4)

        # Reps/Time progression
        if current.reps_target < max_target:
            return Prescription(weight=current.weight, reps_target=min(current.reps_target + target_jump, max_target), sets=current.sets, consecutive_easy_count=0)

        # Weight progression
        if self._strategy.can_increase_weight(config):
            raw_new_weight = self._strategy.bump_weight(current.weight, config)
            rounded = self._strategy.round_to_valid_weight(raw_new_weight, config)
            max_w = config.initial_weight * 2
            clamped = min(rounded, max_w) if max_w > 0 else rounded
            
            if clamped > current.weight:
                return Prescription(weight=clamped, reps_target=config.initial_target, sets=current.sets, consecutive_easy_count=0)

        return Prescription(weight=current.weight, reps_target=current.reps_target, sets=current.sets, consecutive_easy_count=0)

    def _apply_deload(self, config: ExerciseConfig, current: Prescription, severity: int) -> Prescription:
        is_time = (config.target_type == "time")
        new_weight = current.weight
        new_target = current.reps_target

        if is_time:
            new_target = max(15, current.reps_target - (15 * severity))
        else:
            if self._strategy.can_increase_weight(config) and current.weight > 0:
                new_weight = max(current.weight - (config.increment_step * severity), 0.0)
                new_target = config.initial_target
            else:
                new_target = max(config.initial_target, current.reps_target - (2 * severity))
        
        return Prescription(weight=new_weight, reps_target=new_target, sets=current.sets, consecutive_easy_count=0)