# Mutation harness groups

`tests/mutation_check.py` deletes one guard at a time from the real source, runs the suite and restores the file byte-exact. 375 mutants, all caught.

| Group | Mutants |
|---|---|
| The original core guards | 11 |
| Customer memory | 14 |
| Enquiry routing, lead capture and the Zoho client | 40 |
| Chat server and widget | 27 |
| MCP tools, the Zoho reads and the Zia client | 39 |
| Claude API provider | 74 |
| watsonx.ai provider | 120 |
| Discord handoff (`agent/discord_handoff.py`, run with `tests/mutation_check.py Discord`) | 19 |

Six guards each have a test that fails without them: the identity check, the throttle-as-HTTP-200 handling, the legal-threat rule, the grounding guard, the number-word check and `status:active` product filtering.
| WhatsApp lane (`agent/whatsapp_lane.py`, run with `tests/mutation_check.py WhatsApp`) | 17 |
| Notion ticket log (`agent/notion_log.py`, run with `tests/mutation_check.py Notion`) | 14 |
