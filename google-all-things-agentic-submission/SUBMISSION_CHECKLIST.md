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
      boundary (`/health`, `/demo`, `/demo/offline`). The red team module's
      HTTP surface (`POST /redteam`, `GET /quarantine/<incident_id>`) is
      code-complete and tested but **not yet deployed** to this service —
      see the note at the end of this file before recording; do not claim
      it as deployed/live in the video or Devpost form.
- [ ] Capture proof of that Cloud service in both repository and video
      (repository half done via `README.md`; video still pending).

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
- [ ] Do not claim the red team module's HTTP surface (`POST /redteam`,
      `GET /quarantine/<incident_id>`) is deployed/live on Cloud Run — as of
      this checklist it is code-complete and tested only (see note at the
      end of this file).
- [ ] Final human review of IP, third-party logos/content, privacy, and factual
      accuracy.

## Go/no-go

- [ ] All mandatory Google technology requirements evidenced.
- [ ] All mandatory submission fields complete.
- [ ] No unresolved secret or IP exposure.
- [ ] Video and repository accessible to judges.
- [ ] Submit before **31 August 2026, 5:00 PM PDT**.

## Note (2026-08-22, pre-video consolidation)

The red team module (M-1 through M-9, including its `mode="real"` Gemini
attack option added this session) is complete in code, pushed to git, and
covered by tests — but it has **not** been deployed to the live Cloud Run
service (`nexus-google-agentic-demo`). The only thing verified live on that
service is the original demo boundary (`/health`, `/demo`, `/demo/offline`).
Deploying the red team endpoints requires a supervised `gcloud run deploy`
session that has not happened yet. See `NIGHT_QUESTIONS.md`, entry dated
2026-08-22 ("PASO 1: corrección de premisa..."), for the full detail. Do not
record video footage that implies `/redteam` is reachable on the live URL
until that deployment actually happens and is verified with a real request.

