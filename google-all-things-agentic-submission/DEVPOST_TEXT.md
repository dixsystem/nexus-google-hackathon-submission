# Devpost submission text

## Inspiration

Agentic systems are becoming capable of decomposing goals and proposing
complex work. The dangerous assumption is that better reasoning should imply
broader permission. We built this submission around a stricter principle:
**The mission can evolve. Its authority cannot silently change.**

## What it does

NEXUS is a governance layer for autonomous agents. A human supplies a goal.
Gemini generates a structured mission proposal through the Google Gen AI SDK
inside a disposable isolated process. NEXUS/Keeper validates the response
against an exact schema, restricts it to a closed capability registry, derives
trusted identifiers, and writes a durable staging package.

The workflow deliberately stops there. A generated proposal is not approval,
promotion, dispatch, or execution. Those remain behind a separate human
authority boundary that this demo never invokes.

## How we built it

The Google integration uses Gemini through the Google Gen AI SDK. Each request
runs in a fresh child process with a minimal environment. The parent enforces
timeouts and cancellation and can terminate the complete process group.
Response metadata is used for model-identity checks; generated text cannot
self-certify the model that produced it.

After generation, existing NEXUS validation applies an exact JSON contract,
rejects unknown fields, and resolves every requested capability against a
closed registry. Valid candidates become three mutually bound staging
artifacts: a human-readable proposal, execution-contract candidates, and the
full candidate record. No canonical execution state is modified.

The same pipeline has a deterministic offline backend so judges can reproduce
the complete governance path without credentials. Real mode is explicit and
requires both a model ID and an environment-provided credential.

## Challenges

The hardest problem was preserving a hard authority boundary while adding a
network model. Timeouts in a thread are insufficient because abandoned work
can continue. We therefore use process isolation, parent-death supervision,
and termination escalation. We also had to preserve independent model
identity, prevent credential leakage, and ensure malformed or adversarial
model output always fails closed.

## Accomplishments

- Completed a real Gemini-to-staging smoke through the Google Gen AI SDK.
- Preserved one-process-per-request isolation, cancellation, and timeout
  enforcement.
- Demonstrated proposal-to-validation-to-staging without approval, promotion,
  or execution effects.
- Kept offline and real modes on the same governed pipeline.
- Prevented model-generated authority fields from crossing validation.

## What we learned

Model intelligence and system authority are separate architectural concerns.
A schema alone is not a security boundary; the process lifecycle, credential
scope, identity metadata, capability registry, and durable state transitions
must agree. Reproducibility also matters: an offline path can demonstrate
governance deterministically, while a minimal real smoke proves the Google
integration is genuine.

## Red team module

Beyond generating proposals, NEXUS includes an adversarial red-team pipeline
that attacks the same authority boundary instead of exercising it
cooperatively. A second Gemini call plays attacker, deliberately trying to
inject fake authority fields, escalate privileges, or spoof model identity.
Every attempt — blocked or not — is recorded as a hash-chained incident. An
independent severity read from Gemma (a different model, so no model grades
its own work) and an optional independent Gemini self-assessment feed a
triple-filter consensus gate alongside NEXUS's own deterministic verdict;
only unanimous agreement produces a quarantine report with a ready-to-review
corrective recommendation for a human. Nothing here is ever auto-executed:
even an attempt that survives every existing validation layer is recorded as
`VALIDATION_BYPASS` and reported, never run. The module supports a
deterministic offline mode, reproducible without credentials, and an
explicit real-Gemini attack mode with a hard per-session round cap to bound
cost. Its HTTP surface (`POST /redteam`, `GET /quarantine/<incident_id>`) is
deployed and verified on the same live Cloud Run service as the core demo.

## What's next

The core demo boundary and the red team module's HTTP surface are both
deployed and verified live on Cloud Run. Longer term, we want portable
evidence bundles, stronger independent verification, and policy-aware
integrations for more agent runtimes—without ever allowing a producer to
silently expand its own authority.

## Pre-existing work disclosure

NEXUS/Keeper and its core governance mechanisms existed before the contest
submission period. The contest work is the Hackathon Edition: Gemini and
Google Gen AI SDK integration, the isolated Google backend path, local demo
runner, focused tests, Cloud deployment adapter, and submission materials.

