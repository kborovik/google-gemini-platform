# Demo path

Run `gmake generate`, then `gmake deploy`, then the Chat handler.

1. **Generate (`gmake generate`).** Twelve credit policies are already rendered from `corpus/` into `data/credit-policies/`. This recipe runs `uv run docgen generate application --all --local-only` and writes sample applications under `data/client-applications/`.

2. **Deploy (`gmake deploy`).** This recipe runs the hosted path in project `lab5-gemini-dev1`, in this order:
   1. Terraform applies in region `us-east1`. It creates the private document bucket `lab5-gemini-dev1-credit-docs` and the agent service account. It enables `aiplatform.googleapis.com`, `discoveryengine.googleapis.com`, and `storage.googleapis.com`. State stays in the existing bucket `terraform-lab5-gemini-dev1`.
   2. The recipe reads `DATA_STORE` from the Terraform output and writes `agents/credit_officer/.env`. The value is `projects/lab5-gemini-dev1/locations/global/collections/default_collection/dataStores/kb-credit-policies`.
   3. `docgen upload` stores both prefixes in the bucket.
   4. `docgen index --wait` ensures one Agent Search data store, `kb-credit-policies`, in location `global`. It imports `gs://lab5-gemini-dev1-credit-docs/credit-policies/` and `gs://lab5-gemini-dev1-credit-docs/client-applications/`. It then polls until indexed counts meet the floors: at least 12 policies and at least 3 applications.
   5. `uv run adk deploy agent_engine --project=lab5-gemini-dev1 --region=us-east1 --display_name=credit-officer agents/credit_officer` deploys `agents/credit_officer/` to Agent Runtime in `us-east1`. InteractiveAgent calls RetrievalAgent. RetrievalAgent searches that one data store. The agent reads `DATA_STORE` from the environment. Deploy prints a reasoning engine resource name. That resource name is `REASONING_ENGINE`. `adk run` and `adk web` stay local.

3. **Google Chat.** Export `REASONING_ENGINE` to the resource name deploy printed. `uv run python -m docgen.google_chat` listens for a `MESSAGE`. It calls `reasoningEngines/{id}:streamQuery` with class method `async_stream_query`, and posts the model text in that thread. `user_id` is the Chat user. `session_id` is the space plus the thread. The handler holds no policy text. Project and region come from `infra/outputs.json`.

```bash
gmake generate
gmake deploy
export REASONING_ENGINE=<resource name printed by deploy>
uv run python -m docgen.google_chat
```
