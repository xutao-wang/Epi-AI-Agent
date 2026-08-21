# Provider Setup Prompt Design

## Goal

Make native provider setup unambiguous while keeping it short. A user who
enters a hidden API key and presses Enter should understand that validation
starts; a user who submits an empty value should understand that the launcher
returns to provider setup.

## Scope

- Use this provider-specific key prompt for OpenAI and Anthropic:

  ```text
  Paste your <Provider> API key and press Enter to validate
  (empty + Enter returns to provider setup):
  ```

- Remove the combined `Configure both` menu entry. Users configure OpenAI and
  Anthropic independently and may return to the menu to add the second one.
- Keep the application runnable whenever at least one provider verifies.
- Preserve existing keys during reconfiguration unless the user explicitly
  chooses the corresponding remove action.
- Do not change compatible-endpoint behavior or provider validation logic.

## Menu Layout

First-time provider setup:

1. Configure OpenAI
2. Configure Anthropic
3. Connect to a compatible endpoint

Reconfiguration:

1. Configure or replace OpenAI
2. Configure or replace Anthropic
3. Connect to a compatible endpoint
4. Remove OpenAI
5. Remove Anthropic
6. Keep current providers

Submitting an empty API-key prompt returns to the applicable provider menu.
If another provider is already verified, it remains available.

## Testing

- Assert the exact OpenAI and Anthropic prompt text.
- Assert the first-time and reconfiguration menu text and numbering.
- Assert that an empty key returns to provider selection.
- Assert that independently configuring either OpenAI or Anthropic produces a
  valid model catalog.
- Run the focused launcher tests, followed by the full backend suite.
