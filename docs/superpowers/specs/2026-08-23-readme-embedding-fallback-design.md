# README Embedding Fallback Wording Design

## Goal

Correct the README's model-provider guidance so it describes the supported
user configuration without exposing the internal `DB_RAG_EMBEDDING_PROFILE`
selector.

## Design

Replace the existing embedding-profile paragraph with two user-facing facts:

1. Semantic search uses the built-in OpenAI `text-embedding-3-large` model and
   requires `OPENAI_API_KEY`, including when Claude is selected for chat.
2. If OpenAI embeddings are unavailable, search falls back to lexical matching.

The fallback statement intentionally remains general because detailed runtime
messages identify the affected search areas and the specific unavailability
reason.

## Scope and verification

Only `README.md` will change during implementation. Verification will inspect
the rendered Markdown source and confirm that the obsolete instruction to set
`DB_RAG_EMBEDDING_PROFILE` has been removed.
