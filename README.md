# AutoGlific deployment repository

AutoGlific is Product 4: a natural-language flow builder for nonprofit teams
using Glific. This generated repository contains the runtime needed for a
same-origin FastAPI/static deployment and intentionally omits tests, evidence,
local sessions, caches, and unrelated AutoBuilder products.

## Repository layout

- `product4/` is the Product 4 authoring and workbench runtime.
- `product2/` contains only the pinned Product 2 compiler subset required by
  deterministic Engines 2–3. It is not the full Product 2 product.
- `api/index.py` serves the FastAPI API and the static UI from one origin.
- `migrations/001_workbench.sql` defines the durable hosted-storage schema.

The directory names and imports are intentional. Do not rename `product4/` or
`product2/`.

## Pipeline

AutoGlific takes a natural-language brief through node-local clarification and
typed authoring. A confirmed session becomes the frozen `authoring-package-1.0`.
The deterministic pipeline then runs:

1. Engine 1 validates the frozen package and produces a normalized graph.
2. Engine 2 lowers that graph to the pinned Product 2
   `glific-flow-spec-1.0` contract.
3. Engine 3 deterministically compiles the flow spec to Glific JSON.
4. An optional publish step can send that JSON to Glific after separate
   operational review.

## Capability boundary

> **AutoGlific does NOT support all Glific capabilities.**

The authored language is deliberately closed to exactly these five
capabilities:

| Technical name | Plain-language meaning |
| --- | --- |
| `send_text_message` | Send a text message |
| `capture_user_input` | Ask for and capture a user answer |
| `fixed_choice` | Offer a fixed set of choices |
| `persist_contact_field` | Save a captured value to a contact field |
| `end` | End the flow path |

Representative unsupported requests include media/files, external API/webhook
actions, payments, scheduling/waits, loops/joins, advanced conditions, and
other Glific node types. Requests outside this boundary fail closed rather than
being approximated.

## Storage and HTTP surface

Without `DATABASE_URL`, local runs use filesystem storage under the configured
`PRODUCT4_WORKBENCH_DATA` directory. With `DATABASE_URL`, the runtime uses the
transactional Neon/PostgreSQL storage backend; apply the included migration
before starting the hosted runtime. The FastAPI API and static workbench share
one origin, so browser requests do not need a separate API host.

## Local setup and run

From the generated deployment repository root, install the declared runtime
dependencies plus a bounded local ASGI server:

```bash
python -m pip install -r requirements.txt "uvicorn>=0.35,<1"
python -m uvicorn api.index:app --host 127.0.0.1 --port 8000
```

This bundle is an application bundle, not an editable Python package: it has
no `pyproject.toml`, `setup.py`, or top-level `workbench/` package. Run the
ASGI command from the bundle root so `api/` and `product4/` resolve. Use a new
disposable `PRODUCT4_WORKBENCH_DATA` directory for isolated work. Vercel runs
the same `api.index:app` entrypoint.

## Server-side configuration

The following names are the only deployment configuration names documented by
this bundle. Keep their values server-side; `.env.example` contains names only:

```text
DATABASE_URL
OPENAI_API_KEY
PRODUCT4_SEMANTIC_MODEL
GLIFIC_PRODUCTION_BASE_URL
GLIFIC_PHONE
GLIFIC_PASSWORD
```

## Testing

Run the source project's focused hosting, storage, API, engine, and Product 3
independence tests before generating a bundle. Run the Node UI checks and
Python syntax/import checks as well. The generated deployment repository does
not include the test suite or evidence artifacts.

## Vercel and Neon overview

The included `vercel.json` routes `/api/*` and the UI to the same FastAPI
entrypoint and sets the function ceiling used by the publish-lease design. A
hosted setup should use a clean preview Neon database or branch, apply
`migrations/001_workbench.sql` idempotently, set `DATABASE_URL` as a server-side
Vercel variable, and keep the preview protected. The optional publish route
must be reviewed separately from deployment verification.

## Security cautions

Never commit credential values, place them in browser storage, expose them in
logs, or copy them into a bundle. Do not print `DATABASE_URL` or Glific
credentials. Keep preview access protected, leave unused external mutations
disabled, and treat the generated bundle as the complete deployment boundary.
