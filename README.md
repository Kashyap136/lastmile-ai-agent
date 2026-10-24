# LastMile Rescue

A Fetch.ai-powered autonomous surplus-food rescue coordinator: it takes a surplus-food donation through chat, validates it against structured safety rules, matches it to eligible recipient organisations, proposes feasible pickup windows with labelled travel estimates, confirms the plan with the donor, and tracks each donation's full status trail.

Built for Fetch-A-Thon 3.0 with the [uAgents](https://pypi.org/project/uagents/) framework, the [Agent Chat Protocol](https://innovationlab.fetch.ai/resources/docs/agent-communication/agent-chat-protocol) and [ASI:One Interactive Cards](https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards).

## What it does

1. **Donation intake** - the agent opens an Interactive `form` card and collects category, description, quantity/unit, preparation time, pickup deadline, location + demo zone, storage and handling notes.
2. **Validation** - structured parsers reject incomplete/bad submissions with clear field errors; valid data is stored per chat session (recoverable across restarts).
3. **Safety validation** - an explainable rule set (`R01..R09`) approves or rejects with reasons. It never invents certifications; unverifiable food is rejected.
4. **Recipient matching** - ranked, explainable matches against a **clearly-labelled demo dataset** (`carousel` card), considering eligibility, capacity, food suitability, distance and deadline feasibility.
5. **Pickup planning** - feasible service windows for the chosen recipient (`detail` card with radio choice). Travel comes from a real routing API when `ROUTING_API_URL` is configured, otherwise **explicitly-labelled calculated estimates**.
6. **Review & confirmation** - a `review` card requires explicit donor approval. Only after approval is anything recorded as coordinated - and the recipient-notification status is reported honestly (demo data is never contacted).
7. **Status tracking** - ask `status` any time to see the full status trail; reply `picked up` after collection to close the rescue.

## Repository layout

```
src/lastmile/
  config.py            # environment-variable configuration
  models/              # shared typed domain models + demo recipient dataset
  validation/          # input parsers + structured safety rules
  services/            # deterministic matcher / planner / safety services
  adapters/            # routing (OSRM adapter + estimates), recipient notify hook
  cards/               # ASI:One interactive card payload builders
  chat/                # ACP message helpers + card selection parsing
  workflow/            # session state, workflow engine, specialist tools
  protocols/           # agent-to-agent request/response wire models
  agents/              # coordinator uAgent + 3 specialist agents
scripts/
  register_agent.py    # Agentverse programmatic registration
tests/                 # pytest suite (no network or credentials required)
```

## Requirements

- Python 3.10-3.13 (tested on 3.13)
- The `uagents` SDK (`>=0.25.5`) - pinned in `requirements.txt`
- No other credentials required for local development and tests.

## Setup

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env     # then edit .env
```

## Running the coordinator locally

```powershell
$env:PYTHONPATH="src"
python src\lastmile\agents\coordinator.py
```

The agent starts its local HTTP server, joins the **Agentverse mailbox** (`mailbox=True`) and publishes its profile (`publish_agent_details=True`).

## Registering on Agentverse / ASI:One

1. Create an API key: <https://agentverse.ai/profile/api-keys>
2. Put `AGENTVERSE_KEY` and `AGENT_SEED_PHRASE` into `.env` (or environment).
3. With the coordinator running, register:

```powershell
$env:PYTHONPATH="src"
python scripts\register_agent.py
```

On success the agent appears in the Agentverse marketplace. Keep the coordinator process running - the mailbox must stay online for ASI:One interactions.

Verify:
- Agentverse profile: open <https://agentverse.ai> -> Agent -> your agent (profile URL).
- ASI:One: open <https://asi1.ai/chat>, click `@` / mention your agent by name, send "I have surplus food to donate" and use the interactive cards.

## Multi-agent (Layer 3) mode

Three specialist agents expose deterministic, testable services over structured message schemas (`SpecialistRequest`/`SpecialistResponse`):

| Agent | Responsibility | Failure behaviour |
| --- | --- | --- |
| `safety_agent` | Runs the safety rule set, returns structured decisions | Explicit error on bad payload |
| `matcher_agent` | Ranks the demo recipient dataset with explanations | Explicit error on bad payload |
| `planner_agent` | Proposes windows / builds plans with labelled travel estimates | Explicit error on bad payload |

```powershell
$env:AGENT_TOPOLOGY="multi"      # coordinator delegates instead of computing
$env:SAFETY_AGENT_ADDRESS="agent1q..."   # or rely on agent_address_book.json
$env:MATCHER_AGENT_ADDRESS="agent1q..."
$env:PLANNER_AGENT_ADDRESS="agent1q..."
```

Run each agent in its own terminal:

```powershell
python src\lastmile\agents\safety_agent.py
python src\lastmile\agents\matcher_agent.py
python src\lastmile\agents\planner_agent.py
```

In `multi` mode the coordinator enforces **timeouts (20 s) + bounded retries (2)** per call. If a specialist stays unreachable the coordinator reports the explicit failure state, and only then - with a visible `[fallback]` label - computes locally so the demo remains usable.

## Tests

```powershell
$env:PYTHONPATH="src"
.\venv\Scripts\python -m pytest tests -q
```

Covers: complete donation-to-confirmation flow, invalid donations, no matching recipients, card selection parsing (JSON and planner prose), safety-rule outcomes, planner infeasibility, routing-API fallback labelling, notification honesty (never claims success without 2xx), remote-tool timeout/retry behaviour, and session expiry.

## Honesty rules baked into the product

- The recipient dataset is fictional and labelled `(demo)` everywhere it appears.
- Travel estimates are labelled `calculated_estimate` vs `external_api`.
- The status trail only records states the system actually verified; without a notification webhook the donation stays `awaiting_recipient_confirmation`, never `dispatched`.
- Safety decisions carry rule ids and reasons; nothing is auto-certified.

## Demo checklist

1. `hi` -> introduction + donation form card
2. Fill and submit the form (example: 40 meals, prepared 30 min ago, deadline in 4 hours, zone Riverside)
3. Safety validation -> ranked recipients carousel (demo labels)
4. Select a recipient -> feasible windows with travel labels
5. Choose a window -> review card with full plan
6. Approve -> honest receipt + status trail
7. Ask `status` -> full trail; reply `picked up` -> rescue complete
