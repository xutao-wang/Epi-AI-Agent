from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TableRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    study_id: str = Field(min_length=1, max_length=512)
    source_id: str = Field(min_length=1, max_length=512)
    table: str = Field(min_length=1, max_length=512)


class FieldRef(TableRef):
    column: str = Field(min_length=1, max_length=512)


__all__ = ["FieldRef", "TableRef"]
