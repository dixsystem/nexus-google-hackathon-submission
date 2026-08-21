# NEXUS — Governed Agentic Mission Staging

> **The mission can evolve. Its authority cannot silently change.**

NEXUS is a governance layer for autonomous agents. Gemini reasons about a
goal and generates a structured mission proposal. NEXUS/Keeper then applies
closed-world capability validation, model-identity checks, process isolation,
timeouts, cancellation, and fail-closed staging. Generating a proposal never
grants the model approval, promotion, or execution authority.

This submission targets the **Taskmaster** track: it demonstrates a complete
workflow from an unstructured goal to a durable, validated proposal package,
not a chatbot response.

## What the demo proves

1. Gemini produces structured mission data through the Google Gen AI SDK.
2. The SDK call runs in a disposable isolated child process.
3. NEXUS validates the response against an exact schema and a closed
   capability registry.
4. Valid candidates reach staging as three mutually bound artifacts.
5. The workflow stops at `STAGED`. It does not approve, promote, dispatch, or
   execute a mission.

The offline mode exercises the same parent transport, provider, validation,
and staging path with a deterministic mock backend. Real mode changes only
the child backend selected behind that boundary.

## Requirements

- Linux
- The existing isolated Python environment at
  `engineering-loop/.antigravity_isolated_venv`
- Real mode only: `GEMINI_API_KEY` exported in the process environment
- Real mode only: an explicitly selected eligible Gemini model

No credential is accepted as a command-line argument. The runner does not
print it or write it into staging artifacts.

## Run the deterministic offline demo

From the repository root:

```bash
demo_dir="$(mktemp -d /tmp/google-agentic-demo-XXXXXX)"
engineering-loop/.antigravity_isolated_venv/bin/python \
  engineering-loop/google_agentic_demo.py \
  --mode offline \
  --output-dir "$demo_dir"
```

Expected summary:

```text
mode=offline
status=STAGED
candidates=1
authority_effects=NONE
```

## Run one real Gemini smoke

First export the credential outside the repository. Never paste it into a
tracked file or command argument.

```bash
demo_dir="$(mktemp -d /tmp/google-agentic-real-XXXXXX)"
engineering-loop/.antigravity_isolated_venv/bin/python \
  engineering-loop/google_agentic_demo.py \
  --mode real \
  --model '<eligible-gemini-model>' \
  --output-dir "$demo_dir"
```

The accepted real smoke reached:

```text
mode=real
status=STAGED
candidates=1-5 (Gemini real mode is non-deterministic; offline mode always returns exactly 1)
authority_effects=NONE
```

Before submission, replace `<eligible-gemini-model>` with the exact model ID
captured from the accepted smoke evidence and verify that it satisfies the
contest's Gemini 3.5-or-newer requirement.

## Staging output

Each successful run creates:

- `MISSION_PROPOSALS.md`
- `MISSION_PROPOSAL_CONTRACTS.json`
- `MISSION_PROPOSAL_CANDIDATES.json`

These are proposal artifacts, not execution authority. The runner never
touches the canonical mission queue or execution contracts.

## Security properties

- Offline is the default; real Gemini requires explicit opt-in.
- Real mode requires an explicit model and a non-empty `GEMINI_API_KEY`.
- One disposable process is created per request.
- Timeout and cancellation terminate the child process group.
- Unknown fields, malformed responses, model-identity mismatch, and backend
  errors fail closed.
- Credentials, prompts, and generated content are excluded from transport
  evidence; staging contains only the validated proposal contract.
- The model cannot manufacture approval, capability grants, execution
  permission, or evidence verdicts through generated fields.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md).

## Google integration

- Gemini API: real structured generation, verified against gemini-3.5-flash.
- Google Gen AI SDK: the supported Google agent framework used by the isolated backend (google-genai==2.18.1).
- Google Cloud infrastructure: deployed and verified on Cloud Run.

## Deployed Cloud Run service

Live URL: https://nexus-google-agentic-demo-775963240525.us-central1.run.app

Health check: curl https://nexus-google-agentic-demo-775963240525.us-central1.run.app/health
Offline demo: curl -X POST https://nexus-google-agentic-demo-775963240525.us-central1.run.app/demo/offline
Real demo: curl -X POST https://nexus-google-agentic-demo-775963240525.us-central1.run.app/demo

Redeploy from a clean checkout:
1. git clone https://github.com/dixsystem/nexus-google-hackathon-submission.git
2. cd nexus-google-hackathon-submission
3. export GEMINI_API_KEY=your-key-here
4. gcloud config set project your-gcp-project
5. gcloud services enable run.googleapis.com cloudbuild.googleapis.com
6. gcloud run deploy nexus-google-agentic-demo --source=. --region=us-central1 --allow-unauthenticated --set-env-vars=GEMINI_API_KEY=$GEMINI_API_KEY,GEMINI_MODEL=gemini-3.5-flash --port=8080

A root-level Dockerfile (copy of google-all-things-agentic-submission/cloud/Dockerfile) is committed so gcloud run deploy --source=. finds it without extra flags.

## Pre-existing work disclosure

NEXUS/Keeper and its governance mechanisms predate the contest submission
period. The Hackathon Edition work consists of the Google Gemini/Gen AI SDK
integration, isolated Google backend path, local demo runner, focused tests,
Cloud deployment adapter, and submission materials created during the
submission period. Final submission text must preserve this disclosure and
must not imply that the entire underlying governance system was created
during the contest.

## Scope boundary

This demo ends at validated staging. Human approval and any later promotion
or execution are deliberately outside the submission demo.

