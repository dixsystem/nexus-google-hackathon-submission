# Architecture

```mermaid
flowchart LR
    U[Human goal] --> R[Demo runner]
    R -->|offline default| M[Deterministic mock]
    R -->|explicit real opt-in| I[Disposable isolated child]
    I --> G[Google Gen AI SDK]
    G --> X[Gemini]
    M --> P[Untrusted proposal data]
    X --> P
    P --> V[NEXUS exact-schema validation]
    V --> C[Closed capability registry]
    C --> S[STAGED proposal artifacts]
    S -. human gate not invoked .-> A[Approval / promotion / execution]
```

## Authority boundary

Gemini is a proposal producer. It can generate mission content but cannot
alter the authority boundary around that content.

NEXUS/Keeper owns:

- process isolation, deadline enforcement, and cancellation;
- independent model-identity comparison;
- exact response-schema validation;
- closed-world capability selection;
- deterministic mission identifiers and generation hashes;
- durable proposal staging;
- the separate human approval boundary.

The demo never invokes the dotted final edge. A successful result is
`STAGED`, not `APPROVED`, `PROMOTED`, or `EXECUTED`.

> **The mission can evolve. Its authority cannot silently change.**

