# Video script — target 2:30–2:50

The locally saved rules describe an approximately four-minute maximum. This
script deliberately stays below three minutes. Record in English, or provide
accurate English subtitles, and publish the final video on YouTube or Vimeo.

## 0:00–0:20 — Problem

“Autonomous agents are becoming better at planning work. But a better plan
must not silently become broader authority. NEXUS separates reasoning from
permission. The mission can evolve. Its authority cannot silently change.”

Show the title and the one-line architecture.

## 0:20–0:45 — Architecture

“Gemini reasons and generates a structured proposal through Google's Gen AI
SDK. It runs inside a disposable isolated child process. NEXUS/Keeper owns
timeouts, cancellation, model identity, exact validation, the closed
capability registry, and staging. The model never receives approval or
execution authority.”

Show `ARCHITECTURE.md`. Highlight the dotted, non-invoked authority edge.

## 0:45–1:10 — Offline reproducibility

Run the exact offline command from `README.md`.

“Offline mode is the default and deterministic. It uses the same transport,
provider, validator, and staging path, changing only the backend. This lets a
judge reproduce the governance behavior without credentials or network
access.”

Show:

```text
mode=offline
status=STAGED
candidates=1
authority_effects=NONE
```

## 1:10–1:50 — Real Gemini evidence

Show the recorded real-smoke command with the credential hidden and the
actual eligible model ID visible. Do not expose shell history or environment
panes.

“Now the explicit real mode. This is one real Gemini request through the Gen
AI SDK. Gemini returns proposal data; NEXUS independently validates it before
creating staging artifacts.”

Show the non-sensitive smoke record: timestamp, requested and response model
IDs, response ID if present, token counts if present, and artifact hashes.
Then show the staged proposal.

## 1:50–2:15 — Authority proof

“The result is staged, not approved. Generated fields that attempt to claim
approval, a capability token, execution permission, or an evidence verdict
are rejected. The canonical mission queue and execution contracts remain
untouched.”

Show `authority_effects=NONE` and the three staging filenames.

## 2:15–2:35 — Google Cloud proof

Show the selected Google Cloud infrastructure service running the same demo
path. For Cloud Run, show the service page, revision, request/log entry, and
the repository deployment configuration. Do not show billing, credentials,
project secrets, or unrelated services.

“Google Cloud hosts the reproducible demo boundary; it does not replace the
governance layer.”

## 2:35–2:50 — Close

“NEXUS makes autonomous reasoning useful without confusing generation with
authority. Gemini proposes. NEXUS validates. Humans retain the gate. The
mission can evolve. Its authority cannot silently change.”

