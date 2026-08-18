# AutoGlific deployment repository

AutoGlific is Product 4: a same-origin FastAPI/static workbench for creating
small, reviewable Glific flows in plain language. The bundle deliberately
contains only the runtime needed for deployment; tests, evidence, caches, and
unrelated AutoBuilder products are excluded.

`product4/` is the Product 4 authoring and workbench runtime. `product2/` contains only the pinned Product 2 compiler subset.

## Authentication and account settings

The hosted UI uses native email + password accounts. Registration collects a
display name, email, and password; login uses only email and password. A successful login issues
an opaque server-side session token in an `HttpOnly`, `Secure`, `SameSite=Lax`
cookie. Mutating requests also require a CSRF double-submit token. Passwords
are Argon2-hashed. Login failures receive a small in-process rate limit.
Legacy accounts without a stored display name are idempotently backfilled to
the neutral label `Account`; the configured bootstrap owner name replaces that
neutral label without changing the immutable internal `user_id` ownership key.

The identity boundary is provider-independent: `workbench/auth.py` exposes an
`IdentityProvider` seam, and the current adapter is intentionally
`NativeEmailPasswordProvider`. It does not reverse-engineer ChatGPT/Codex
OAuth, expose OpenAI tokens, or treat an OpenAI API key as user identity.
Official OpenAI documentation establishes API-key/bearer authentication for
API requests and says API keys are secrets; it does not establish a general
“Sign in with ChatGPT” provider for this standalone app. See the [OpenAI API
authentication reference](https://developers.openai.com/api/reference/overview#authentication).

After the first authentication, a compact setup wizard collects credentials in
this order: Glific base URL, Glific mobile number, Glific password, OpenAI API
key plus an optional OpenAI project ID, then a masked review/save step. Each user can save:

- their OpenAI API key, used only server-side for authoring;
- an optional OpenAI project ID, used only to select the project for that key;
- the fixed authoring model `gpt-5.6-sol` (there is no model selector);
- their Glific HTTPS tenant URL, mobile number, and password.

Glific fields may be edited later and are required before publishing.
Credentials are encrypted with the deployment key
`PRODUCT4_CREDENTIAL_ENCRYPTION_KEY` using Fernet. Settings responses contain
only non-secret project metadata, configuration flags, and masks; blank replace
fields preserve existing values, while `clear_*` actions explicitly remove
them. Secrets are never
placed in browser storage or returned to the browser.

The flow library contains one shared, immutable demo: the exact authored
`Sakhi NGO MCH Demo` session fixture under
`workbench/preloaded/sakhi-ngo-mch-demo.json`. It is available to anonymous and
authenticated visitors for review with its validated publication details, but
cannot be edited, re-published, or downloaded. **+ New flow** is the
authentication trigger for a private, owner-scoped flow. The
native adapter is the replaceable decision point for a future officially
documented identity provider.

## One end-to-end flow example

1. Open AutoGlific and click **Start**. The shared flow library opens with the
   Sakhi NGO MCH Demo available for review.
2. Click **+ New flow**, choose **Register**, and create an account with a
   full name, email, and an 8–128 character password.
3. Complete the setup wizard in its displayed order: Glific URL, mobile number,
   Glific password, OpenAI API key, then review and save. Accounts that already
   have both OpenAI and Glific bootstrap credentials skip this wizard. Cancel
   closes it without creating a flow; it can be resumed from **+ New flow** or Settings.
4. Name the flow `Maternal support`.
5. Enter: `Welcome families with a clear maternal and child health support
   message.` Answer the clarification with the exact message you want to
   send.
6. Enter: `Ask what support they need and offer Pregnancy, Newborn care, or
   Nutrition.` Answer any requested choice wording/options. Add the next
   branch instructions one at a time, for example:
   `Send the pregnancy support message and end this branch.`
7. Click **Review flow**, inspect the Logic and Flowchart panels, then click
   **Confirm flow**. AutoGlific freezes the approved package, runs the
   deterministic pipeline, and shows the generated Glific artifact.
8. Click **Publish**. A flow is reported as published only after Glific returns
   a usable confirmed flow identity.

## Pipeline and supported capabilities

The authoring boundary is deliberately closed. A confirmed session becomes a
frozen `authoring-package-1.0`; the deterministic pipeline then runs Engine 1 graph
normalization, the pinned Engine 2 flow specification, and Engine 3 Glific JSON
compilation before optional publication.

| Supported capability | Meaning |
| --- | --- |
| `send_text_message` | Send a text message |
| `capture_user_input` | Ask for and capture a user answer |
| `fixed_choice` | Offer a fixed set of choices |
| `persist_contact_field` | Save a captured value to a contact field |
| `end` | End the flow path |

AutoGlific does NOT support all Glific capabilities. Unsupported capabilities
fail closed rather than being approximated: media/files, external API/webhook
actions, payments, scheduling/waits, loops/joins, advanced conditions, and
other Glific node types. The authoritative boundary
is implemented by the registry/interpreter and exercised by the authoring and
pipeline tests; see `tests/test_workbench_semantic.py`,
`tests/test_p40_boundaries.py`, and `tests/test_p50_acceptance.py`.

## Storage, configuration, and local run

Without `DATABASE_URL`, local runs use filesystem storage under
`PRODUCT4_WORKBENCH_DATA`. With `DATABASE_URL`, use the PostgreSQL/Neon
backend and apply `migrations/001_workbench.sql` before starting. The
migration is idempotent and adds users, opaque auth sessions, encrypted
credential ciphertext, and nullable ownership metadata for legacy rows.
Legacy unowned rows remain preserved but are not exposed through authenticated
owner-scoped routes.

Required hosted configuration:

```text
DATABASE_URL=
PRODUCT4_CREDENTIAL_ENCRYPTION_KEY=
PRODUCT4_BOOTSTRAP_EMAIL=
PRODUCT4_BOOTSTRAP_PASSWORD=
PRODUCT4_BOOTSTRAP_NAME=
PRODUCT4_BOOTSTRAP_OPENAI_API_KEY=
PRODUCT4_BOOTSTRAP_OPENAI_PROJECT_ID=
PRODUCT4_BOOTSTRAP_GLIFIC_BASE_URL=
PRODUCT4_BOOTSTRAP_GLIFIC_PHONE=
PRODUCT4_BOOTSTRAP_GLIFIC_PASSWORD=
PRODUCT4_BOOTSTRAP_OPENAI_ROTATE=
PRODUCT4_BOOTSTRAP_CREDENTIALS_ROTATE=
```

For local use, the server loads a mode-600, ignored `.env.local` file without
overriding process environment variables. It also reads only the approved
OpenAI/Glific compatibility names from the ignored AutoBuilder parent `.env`;
that file is never copied into a bundle. Local runs create a random Fernet key
at `.workbench-data/credential-encryption.key` with mode 600 when a deployment
key is not supplied. Hosted/Vercel runs do not create a local key and fail
closed unless `PRODUCT4_CREDENTIAL_ENCRYPTION_KEY` is configured.

Hosted bootstrap credential seeding uses the explicit
`PRODUCT4_BOOTSTRAP_OPENAI_API_KEY`, optional paired
`PRODUCT4_BOOTSTRAP_OPENAI_PROJECT_ID`, `PRODUCT4_BOOTSTRAP_GLIFIC_BASE_URL`,
`PRODUCT4_BOOTSTRAP_GLIFIC_PHONE`, and `PRODUCT4_BOOTSTRAP_GLIFIC_PASSWORD`
secrets. Local bootstrap prefers the paired `LLM_API_KEY` and `LLM_PROJECT_ID`
from the ignored parent `.env`, then falls back to `OPENAI_API_KEY` without a
project header, plus `GLIFIC_PRODUCTION_BASE_URL`, `GLIFIC_PHONE`, and
`GLIFIC_PASSWORD`. A project ID is never mixed with a key from another source.
Only missing fields for the configured bootstrap account are seeded. Set
`PRODUCT4_BOOTSTRAP_OPENAI_ROTATE=1` for an explicit one-time replacement of
only the configured owner's OpenAI key/project pair; use
`PRODUCT4_BOOTSTRAP_CREDENTIALS_ROTATE=1` only when all configured bootstrap
fields should be replaced. `PRODUCT4_BOOTSTRAP_NAME` supplies the owner's
display name. Bootstrap is idempotent and the password is only stored as an
Argon2 hash.

Generate the encryption key once and store it as a deployment secret:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

The compatibility variables in `.env.example` (`OPENAI_API_KEY` and the
`GLIFIC_*` values) support direct local/offline runtime tests. Authenticated
hosted requests use each user’s encrypted settings instead of those ambient
values. Do not commit any credential value.

From the generated deployment repository root:

```bash
python -m pip install -r requirements.txt "uvicorn>=0.35,<1"
python -m uvicorn api.index:app --host 127.0.0.1 --port 8000
```

This is an application bundle, not an editable Python package. Run the ASGI
command from the bundle root so `api/` and `product4/` resolve. Vercel uses
the same `api.index:app` entrypoint declared in `pyproject.toml`.

Automated local fixtures use the explicit `WorkbenchApp(offline=True)` seam;
that test mode is not enabled by the hosted entrypoint and must not be used as
the production authentication configuration.

## Known limitations and evidence

- Email verification and password reset are not implemented in this iteration:
  the native adapter currently exposes only register, login, logout, and
  session resolution (`workbench/auth.py`, `api/index.py`).
- Brute-force limiting is process-local, so a multi-instance deployment needs
  a shared limiter or edge control (`_LoginRateLimiter` in
  `workbench/auth.py`).
- A deployment key is required to create or replace encrypted credentials. If
  it is missing or invalid, settings/runtime credential use fails closed with
  a safe error (`workbench/credentials.py`). Losing the key makes ciphertext
  unrecoverable.
- Only the five capabilities above are supported. This is a product boundary,
  not a promise that arbitrary Glific JSON can be imported; unsupported
  requests fail closed in the authoring/pipeline tests cited above.
- Glific publishing depends on the tenant’s documented import/publish
  behavior and network availability. The client validates HTTPS tenant-origin
  URLs and saves only the confirmed public flow identity; provider failures are
  not treated as success (`workbench/glific_client.py`,
  `workbench/server.py`).
- The shared demo is sourced from the tracked
  `workbench/preloaded/sakhi-ngo-mch-demo.json` fixture, validated through
  `AuthoringSession` parsing. Its sanitized publication binding is tracked at
  `workbench/preloaded/sakhi-ngo-mch-demo-publication.json`; both are copied
  into hosted bundles. It is never used as a user-owned session.
- No official standalone ChatGPT identity-provider adapter is claimed here.
  Selecting and configuring a future provider remains an explicit product and
  deployment decision; the provider-independent ownership/storage seam is in
  place for that decision.

## Testing

Useful focused checks are:

```bash
../.venv/bin/python -m pytest -q tests/test_native_auth_boundary.py
../.venv/bin/python -m pytest -q tests/test_workbench.py tests/test_hosting_bundle.py tests/test_storage.py
node --test tests/test_workbench_ui_runtime.mjs
node --check workbench/static/app.js
```

The source repository’s broader suite also covers the authoring contract,
deterministic engines, safe errors, and bundle independence. The generated
deployment bundle intentionally does not include that suite.

## Security cautions

Never commit credential values, put secrets in browser storage, expose them in
logs, or copy them into a bundle. Keep the deployment key stable and private,
protect preview access, and treat the generated bundle as the complete
deployment boundary.
