"""Generate n8n/workflow.json. JSON is written by a script, not by hand, so node names and connections cannot drift.

Flow:  Webhook -> Ask the support agent (HTTP) -> Needs a person?
         yes -> Hand off to a human (Code) -> Respond with the holding reply
         no  -> Respond with the agent's reply

The "Hand off to a human" node builds the ticket a helpdesk node would receive. It is a Code node on
purpose: wiring a real Gorgias, Zendesk or Slack node needs the store's own credentials, and the
shape of the handoff is the part worth proving here.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_URL = "http://host.docker.internal:8008/support"

nodes = [
    {"id": "n1", "name": "Customer message", "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [0, 0],
     "webhookId": "support-agent-demo",
     "parameters": {"httpMethod": "POST", "path": "support", "responseMode": "responseNode", "options": {}}},
    {"id": "n2", "name": "Ask the support agent", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
     "position": [260, 0],
     "parameters": {
         "method": "POST", "url": AGENT_URL, "sendHeaders": True,
         "headerParameters": {"parameters": [{"name": "X-Agent-Key", "value": "={{ $env.SUPPORT_AGENT_KEY }}"}]},
         "sendBody": True, "specifyBody": "json",
         "jsonBody": "={{ JSON.stringify({ message: $json.body.message, email: $json.body.email }) }}",
         "options": {"timeout": 90000}}},
    {"id": "n3", "name": "Needs a person?", "type": "n8n-nodes-base.if", "typeVersion": 1, "position": [520, 0],
     "parameters": {"conditions": {"string": [
         {"value1": "={{ $json.action }}", "operation": "equal", "value2": "escalated"}]}}},
    {"id": "n4", "name": "Hand off to a human", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [780, -120],
     "parameters": {"jsCode": (
         "const r = $input.first().json;\n"
         "const sent = $('Customer message').first().json.body;\n"
         "// the ticket a helpdesk node would receive: who, why, what the agent already did. No order details.\n"
         "return [{ json: {\n"
         "  ticket: { customer_email: sent.email, reason: r.escalation_reason, intent: r.intent,\n"
         "            agent_steps: r.steps, customer_message: sent.message },\n"
         "  action: r.action, reply: r.reply, escalation_reason: r.escalation_reason } }];")}},
    {"id": "n5", "name": "Reply, escalated", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1,
     "position": [1040, -120],
     "parameters": {"respondWith": "json",
                    "responseBody": "={{ JSON.stringify({ action: $json.action, reply: $json.reply, escalation_reason: $json.escalation_reason, ticket_created: true }) }}"}},
    {"id": "n6", "name": "Reply, answered", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1,
     "position": [780, 120],
     "parameters": {"respondWith": "json",
                    "responseBody": "={{ JSON.stringify({ action: $json.action, reply: $json.reply, order: $json.order, ticket_created: false }) }}"}},
]

connections = {
    "Customer message": {"main": [[{"node": "Ask the support agent", "type": "main", "index": 0}]]},
    "Ask the support agent": {"main": [[{"node": "Needs a person?", "type": "main", "index": 0}]]},
    "Needs a person?": {"main": [[{"node": "Hand off to a human", "type": "main", "index": 0}],
                                 [{"node": "Reply, answered", "type": "main", "index": 0}]]},
    "Hand off to a human": {"main": [[{"node": "Reply, escalated", "type": "main", "index": 0}]]},
}

# every connection must name a node that exists, or n8n imports it and silently drops the edge
names = {n["name"] for n in nodes}
for src, v in connections.items():
    assert src in names, src
    for branch in v["main"]:
        for edge in branch:
            assert edge["node"] in names, edge["node"]

workflow = {"id": "support-agent-demo", "name": "Shop support agent", "nodes": nodes, "connections": connections,
            "active": False, "settings": {"executionOrder": "v1"}, "pinData": {}}
(HERE / "workflow.json").write_text(json.dumps(workflow, indent=2) + "\n", encoding="utf-8")
print("wrote", HERE / "workflow.json", "with", len(nodes), "nodes")
