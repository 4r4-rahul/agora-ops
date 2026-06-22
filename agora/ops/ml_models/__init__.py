"""
agora/ops/ml_models/ — the model fleet (M1..M8). Each module defines a pure fn(db_path)->dict and
self-registers into model_runner.MODEL_REGISTRY at import. The runner imports this package once so
every model is registered; importing here is idempotent (register_model dedupes by name).

All models are READ-ONLY on trading data and write only through the model-runner's score/run tables —
they cannot affect execution.
"""
from __future__ import annotations

# Importing each module triggers its register_model() call.
from agora.ops.ml_models import (
    m1_fill,  # noqa: F401
    m3_liquidity,  # noqa: F401
    m4_winrate,  # noqa: F401
    m5_conviction,  # noqa: F401
    m8_lifecycle,  # noqa: F401
)
