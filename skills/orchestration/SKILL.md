# orchestration

Autonomous worker-critic feedback loops: headless claude -p turns, verdict-gated resumes, human override.

Trigger: /orchestrate
Status: learning (earns after 3 uses via `python skills/engine.py use orchestration`)

## Process

1. Hermes first: `python hermes/hermes.py query "<mission>"` - reuse prior loops
2. Launch from the dashboard (Orchestrated mode) or POST /api/orchestrate
3. Worker runs headless with a turn budget; session resumes between rounds
4. The critic is the core (opus) - it reads the report and rules accept/revise/reject
5. Untick auto-accept to gate every verdict on Daniel; Stop kills the running turn
6. Verdicts, costs, and every round land on the wire under the loop's oid
