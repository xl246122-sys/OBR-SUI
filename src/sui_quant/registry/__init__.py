"""E-01 Signed Model Artifact Registry, SHADOW -> PAPER Promotion, AutoRollbackGuard,
In-Flight Position Version Pinning, and D-01 Integrated Pipeline (§11.2, §13, QA-12)."""

from sui_quant.registry.e01_model_registry import (
    E01ArtifactPackager,
    E01ModelRegistry,
    InFlightPositionPinState,
    InFlightVersionPinRegistry,
    ModelRegistryVerificationError,
    ModelSlotRole,
    RollbackAuditRecord,
    ShadowInferenceRecord,
    SignedModelArtifactBundle,
    make_bundle_id,
)
from sui_quant.registry.integrated_pipeline import (
    D01IntegratedPipeline,
    IntegratedCycleReport,
)

__all__ = [
    "D01IntegratedPipeline",
    "E01ArtifactPackager",
    "E01ModelRegistry",
    "InFlightPositionPinState",
    "InFlightVersionPinRegistry",
    "IntegratedCycleReport",
    "ModelRegistryVerificationError",
    "ModelSlotRole",
    "RollbackAuditRecord",
    "ShadowInferenceRecord",
    "SignedModelArtifactBundle",
    "make_bundle_id",
]
