# Final submission checklist

## Eligibility and track

- [ ] Select **Taskmaster** in Devpost.
- [ ] Confirm entrant age, jurisdiction, and no disqualifying Google/Devpost
      employment or contractor relationship.
- [ ] Confirm whether the Startup Excellence category applies; do not claim it
      without an incorporated organization and qualifying corporate email.
- [ ] Preserve the pre-existing-work disclosure verbatim or equivalently.
- [ ] Verify the exact rules page wording before final submit where the saved
      evidence is partial: video limit, private-repository clause, and reuse of
      pre-existing work.

## Required Google technology

- [x] Real Gemini call completed through the Gemini API.
- [x] Google Gen AI SDK integrated as the selected Google framework.
- [ ] Record the exact requested and response model IDs from the accepted
      smoke and prove the model satisfies **Gemini 3.5 or newer**.
- [x] Use at least one Google Cloud infrastructure service — Cloud Run,
      live and verified: https://nexus-google-agentic-demo-775963240525.us-central1.run.app
      (see `README.md`, "Deployed Cloud Run service"). Covers the core demo
      boundary (`/health`, `/demo`, `/demo/offline`) **and** the red team
      module's HTTP surface (`POST /redteam`, `GET /quarantine/<incident_id>`)
      — confirmed live with a real successful `POST /redteam` call (offline
      mode, no quota spent); see the note at the end of this file for the raw
      evidence.
- [x] Capture proof of that Cloud service in both repository and video
      (repository proof: see the note at the end of this file; video still
      pending).

## Reproducibility and evidence

- [x] Offline demo command documented.
- [x] Real demo command documented without embedding credentials.
- [x] Successful flow ends at `STAGED` with `authority_effects=NONE`.
- [ ] Add a sanitized real-smoke record containing only timestamp, model ID,
      response ID if available, token counts if available, and staging paths/
      hashes.
- [ ] Run focused tests from a clean submission checkout.
- [ ] Confirm no staging artifact, log, screenshot, shell history, or video
      frame contains a credential.
- [ ] Confirm the canonical mission queue and execution contracts are unchanged
      by both demo modes.

## Repository

- [ ] Choose public or private repository.
- [ ] If private, manually verify the rule and grant access to
      `testing@devpost.com` and `cloudhackathons@google.com`.
- [ ] Include the runnable source, dependency lock/constraints, tests, README,
      architecture diagram, deployment configuration, and sanitized evidence.
- [ ] Exclude `.env`, credentials, private Keeper internals not required for the
      submission, unrelated journals, and workstation artifacts.
- [ ] Run a secret scan and inspect its findings before sharing access.
- [ ] Verify every claim against an artifact or test.

## Video

- [ ] Keep final duration between 2:00 and 3:00, safely below the locally
      recorded approximately four-minute maximum.
- [ ] Record in English or add accurate English subtitles.
- [ ] Show the problem and value proposition.
- [ ] Show the architecture and authority boundary.
- [ ] Show offline reproducibility.
- [ ] Show sanitized real Gemini evidence.
- [ ] Show visual proof that the backend uses Google Cloud infrastructure.
- [ ] Show `STAGED` and `authority_effects=NONE`.
- [ ] Upload publicly to YouTube or Vimeo and test the URL logged out.

## Devpost form

- [ ] Paste and tailor all sections from `DEVPOST_TEXT.md`.
- [ ] Add repository URL and video URL.
- [ ] Add the architecture diagram.
- [ ] List Gemini API, Google Gen AI SDK, and the selected Google Cloud service.
- [ ] Do not claim fleet governance, cross-department cataloging, weeks of
      autonomous operation, or production execution.
- [ ] Final human review of IP, third-party logos/content, privacy, and factual
      accuracy.

## Go/no-go

- [ ] All mandatory Google technology requirements evidenced.
- [ ] All mandatory submission fields complete.
- [ ] No unresolved secret or IP exposure.
- [ ] Video and repository accessible to judges.
- [ ] Submit before **31 August 2026, 5:00 PM PDT**.

## Note (2026-08-22, pre-video consolidation — corrected same day)

An earlier version of this note incorrectly stated that the red team module
was not deployed to the live Cloud Run service. That was based only on
indirect evidence (README/ARCHITECTURE text and this session's own
conversation history, neither of which can see a `gcloud run deploy` run
outside of git in an earlier session). The user then reported testing the
live service directly and getting a real success response; this was
independently re-verified with a safe, quota-free request:

```
$ curl -sf -w '\nHTTP:%{http_code}\n' -X POST https://nexus-google-agentic-demo-775963240525.us-central1.run.app/redteam
{"authority_effects":"NONE","escalated_incident_ids":[],"incident_count":5,"mode":"offline","quarantine_report":"# NEXUS RED TEAM ...","rounds":5,"session_id":"redteam-20260822T190835","status":"COMPLETED","timestamp":"2026-08-22T19:08:39.369883+00:00","validation_bypass_count":5}
HTTP:200
```

The `"mode":"offline"` field in that response only exists in code added
during this session (commit `9d00309`), which confirms the live service is
running that revision (or later) — the red team module's HTTP surface,
including the `mode="real"` option, is genuinely deployed. Only the offline
path was independently re-verified here (no real Gemini quota spent by this
check); `mode="real"` was not separately re-tested to avoid spending quota,
but runs through the same deployed code path. See `NIGHT_QUESTIONS.md`,
entry dated 2026-08-22 ("PASO 1 corrección (v2)"), for full detail.

