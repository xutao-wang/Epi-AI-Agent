from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


def dataset_storage_content(dataset: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(dataset, Mapping):
        raise ValueError("Dataset storage content must be a mapping")
    content = deepcopy(dict(dataset))
    content.pop("status", None)
    provenance = content.get("provenance")
    if provenance is not None:
        if not isinstance(provenance, Mapping):
            raise ValueError("Dataset provenance must be a mapping")
        normalized = deepcopy(dict(provenance))
        normalized.pop("status_transitions", None)
        content["provenance"] = normalized
    return content
