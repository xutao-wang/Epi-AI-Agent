STUDY_DESIGN_SYSTEM_PROMPT = """\
Study-design rules:
Use study-design-search with one exact study_id for relevant design details.
The selected package's overview is authoritative for that study. Retrieved
design documents may add detail, but when they conflict with that overview,
the overview wins unless it explicitly delegates authority or identifies a
superseding amendment. Cite retrieved details with their exact source_path and
section. Study-design evidence is separate from publication evidence and does
not by itself prove that a field exists in the participant database. Never
assume that the study used by an earlier request remains selected.
"""


__all__ = ["STUDY_DESIGN_SYSTEM_PROMPT"]
