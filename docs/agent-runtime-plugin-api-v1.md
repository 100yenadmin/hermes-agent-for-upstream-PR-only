# AgentRuntime Plugin API v1

This legacy path is retained for links and historical navigation. It is not a
second contract.

Read the [canonical AgentRuntime v1 ADR](adr/agent-runtime-v1.md) for the
frozen Revision 4 host boundary, including:

- Hermes ownership of prompt/messages, memory, skills, tools, approval,
  execution, delegation, background work, transcript, state, visible lifecycle,
  and usage receipts;
- the provider-plugin `tools=[]`, `setting_sources=[]`, no-preset/no-native-
  `Agent` contract and strict `mcp__hermes-tools__*` exposure;
- prompt equality, pre-effect paired tool transcript persistence, host content
  streaming, and typed terminal events; and
- the honest at-least-once adapter boundary with idempotent durable consumers.

Current implementation source before the documentation-only restamp:
`0b1dea57f303d2db5d2e9099254a663e7cc8faa8`.

Do not add requirements, provider policy, or implementation detail here; update
the ADR and its [canonical coupling map](architecture/agent-runtime-v1-coupling-map.md)
if the architecture changes.
