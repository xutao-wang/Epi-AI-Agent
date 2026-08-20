from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class StudySourceUnavailableError(RuntimeError):
    """A configured study data source cannot be opened."""


class StudyKnowledgeProvider(Protocol):
    pass


class StudyDesignProvider(Protocol):
    def render_context(self) -> str:
        """Return bounded authoritative context for the active study."""


@runtime_checkable
class SearchableStudyDesignProvider(StudyDesignProvider, Protocol):
    def search(self, query: str, limit: int = 5) -> tuple[object, ...]:
        """Return bounded study-design retrieval hits."""


class StudyCatalogProvider(Protocol):
    pass


class StudyDataSource(Protocol):
    pass


@dataclass(frozen=True)
class StudyBundle:
    study_id: str
    label: str
    knowledge: StudyKnowledgeProvider | None
    catalog: StudyCatalogProvider | None
    data_sources: Mapping[str, StudyDataSource]
    study_design: StudyDesignProvider | None = None
    package_version: str = ""
    description: str | None = None
    source_id: str = ""
    db_rag_paths: object | None = None
    study_overview: StudyDesignProvider | None = None


class StudyRegistry:
    def __init__(self, studies: Iterable[StudyBundle] = ()) -> None:
        self._studies: dict[str, StudyBundle] = {}
        for study in studies:
            self.register(study)

    def register(self, study: StudyBundle) -> None:
        if study.study_id in self._studies:
            raise ValueError(f"Duplicate study: {study.study_id}")
        self._studies[study.study_id] = study

    def get(self, study_id: str) -> StudyBundle | None:
        return self._studies.get(study_id)

    @property
    def values(self) -> tuple[StudyBundle, ...]:
        return tuple(self._studies.values())

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._studies))

    def require(self, study_id: str) -> StudyBundle:
        try:
            return self._studies[study_id]
        except KeyError as error:
            raise KeyError(f"Unknown study: {study_id}") from error
