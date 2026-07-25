"""Public `feature_eng` tool surface (spec.md: backend/tools/ is ML-owned).

The implementation lives in `ml/feature_eng.py`. The twelve ML modules cross-import each other
(`ml.data`, `ml.features`, `ml.cache`), so the five public tools stay there rather than being
split away from their support modules; this package is the documented entry point.

Register tools from `ml.tools` (name -> callable, with the matching JSON schemas), not from
here — `agent/loop.py` does exactly that.
"""

from ml.feature_eng import feature_eng

__all__ = ["feature_eng"]
