"""P3 Scientific Modeling Layer (`A-01` Market Regime Classifier & `A-02` Triple-Barrier Calibrated Breakout Model)."""

from __future__ import annotations

from sui_quant.models.a01_regime import (
    A01RegimeClassifier,
    A01RegimeConfig,
    A01RegimeDiagnostics,
)
from sui_quant.models.a02_breakout import (
    A02_FEATURE_NAMES,
    A02BreakoutCalibratedModel,
    A02CostAssumptions,
    ModelCalibrationGateError,
    compute_multiclass_calibration_metrics,
    project_to_decimal_simplex,
)
from sui_quant.models.features import (
    STANDARD_MODEL_FEATURE_NAMES,
    CausalityViolationError,
    FeaturePipeline,
    FeaturePipelineConfig,
    UnclosedBarRejectedError,
    compute_feature_snapshot_hash,
    compute_features_schema_hash,
    load_closed_bars_from_warehouse,
)
from sui_quant.models.triple_barrier import (
    BarrierOutcome,
    BreakoutCandidateCheck,
    BreakoutCandidateConfig,
    PurgedWalkForwardFold,
    PurgedWalkForwardSplitter,
    TripleBarrierConfig,
    TripleBarrierLabel,
    TripleBarrierLabeler,
    evaluate_breakout_candidate,
)

__all__ = [
    "A01RegimeClassifier",
    "A01RegimeConfig",
    "A01RegimeDiagnostics",
    "A02BreakoutCalibratedModel",
    "A02CostAssumptions",
    "A02_FEATURE_NAMES",
    "BarrierOutcome",
    "BreakoutCandidateCheck",
    "BreakoutCandidateConfig",
    "CausalityViolationError",
    "FeaturePipeline",
    "FeaturePipelineConfig",
    "ModelCalibrationGateError",
    "PurgedWalkForwardFold",
    "PurgedWalkForwardSplitter",
    "STANDARD_MODEL_FEATURE_NAMES",
    "TripleBarrierConfig",
    "TripleBarrierLabel",
    "TripleBarrierLabeler",
    "UnclosedBarRejectedError",
    "compute_feature_snapshot_hash",
    "compute_features_schema_hash",
    "compute_multiclass_calibration_metrics",
    "evaluate_breakout_candidate",
    "load_closed_bars_from_warehouse",
    "project_to_decimal_simplex",
]
