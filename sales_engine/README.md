# Path-Flow Sales Engine MVP

Campaign #001: Nail salons / 113 stores.

Goal: automate lead state management, scheduled follow-ups, response detection handoff, and daily exception reporting without touching the public LP runtime.

## MVP states

- READY
- SENT
- FOLLOWUP_1
- FOLLOWUP_2
- RESPONDED
- HUMAN_ACTION
- CLOSED_NO_RESPONSE
- WON_ONE_TIME
- WON_MAINTENANCE
- LOST

## Safety rules

1. Dry-run is the default. Real sending must be explicitly enabled.
2. Any response stops automated follow-ups immediately.
3. Unknown/ambiguous responses must be escalated to HUMAN_ACTION.
4. Duplicate sends are prevented by event history.
5. Campaign analysis and sending cadence are separated: send broadly, segment later for analysis.

## Local runtime

This folder is intended to run on the local Windows PC with Python. Ollama is optional and used only for response classification. Core scheduling/state transitions are deterministic and do not depend on AI.
