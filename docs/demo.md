# Demo path

Run `gmake generate`, then `gmake deploy`, then `gmake chat`.

1. **Generate (`gmake generate`).** Twelve credit policies are already rendered from `corpus/` into `data/credit-policies/`. This recipe runs `uv run docgen generate application --all --local-only` and writes sample applications under `data/client-applications/`.

2. **Deploy (`gmake deploy`).** This recipe runs the hosted path in project `lab5-gemini-dev1`, in this order:
   1. Terraform applies in region `us-east1`. It creates the private document bucket `lab5-gemini-dev1-credit-docs`, the empty Agent Search data store `kb-credit-policies` in location `global`, the agent service account `credit-policy-agent`, and the Reasoning Engine `credit-officer`. The engine runs `agents/credit_officer` on Agent Runtime in `us-east1`. `DATA_STORE` is set on the engine. Output `REASONING_ENGINE` is `projects/lab5-gemini-dev1/locations/us-east1/reasoningEngines/{id}`, written to `infra/outputs.json`. It enables `aiplatform.googleapis.com`, `discoveryengine.googleapis.com`, and `storage.googleapis.com`. State stays in the existing bucket `terraform-lab5-gemini-dev1`. `adk run` and `adk web` stay local.
   2. The recipe reads `DATA_STORE` from the Terraform output and writes `agents/credit_officer/.env`. The value is `projects/lab5-gemini-dev1/locations/global/collections/default_collection/dataStores/kb-credit-policies`.
   3. `docgen upload` stores both prefixes in the bucket.
   4. `docgen index --wait` imports `gs://lab5-gemini-dev1-credit-docs/credit-policies/` and `gs://lab5-gemini-dev1-credit-docs/client-applications/` into data store `kb-credit-policies`. It does not create the store. It then polls until indexed counts meet the floors: at least 12 policies and at least 3 applications.

3. **Google Chat (`gmake chat`).** `gmake chat` runs `uv run python -m docgen.google_chat` on `127.0.0.1` and publishes it with cloudflared. Paste the `trycloudflare.com` HTTPS URL into the Chat app HTTP endpoint. The handler listens for a `MESSAGE`. It calls `reasoningEngines/{id}:streamQuery` with class method `async_stream_query`, and posts the model text in that thread. `user_id` is the Chat user. `session_id` is the space plus the thread. The handler holds no policy text. Project, region, and `REASONING_ENGINE` come from `infra/outputs.json`.

```bash
gmake generate
gmake deploy
gmake chat
```
