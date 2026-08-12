STUDY_DESIGN_SYSTEM_PROMPT = """\
Study-design rules:
The active study's overview is authoritative always-on context. Use
study-design-search for relevant details not present in the overview. Retrieved
design documents may add detail, but when they conflict with the overview, the
overview wins unless it explicitly delegates authority or identifies a
superseding amendment. Cite retrieved details with their exact source_path and
section. Study-design evidence is separate from publication evidence and does
not by itself prove that a field exists in the participant database.
"""


__all__ = ["STUDY_DESIGN_SYSTEM_PROMPT"]
