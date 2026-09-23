# Demo path

Five steps take this repository to a cited answer in Google Chat.

1. **Google Cloud (`gmake terraform-apply`).** Terraform applies in project `lab5-gemini-dev1`, region `us-east1`. It creates the private document bucket `lab5-gemini-dev1-credit-docs` and the agent service account. It enables `aiplatform.googleapis.com`, `discoveryengine.googleapis.com`, and `storage.googleapis.com`. State stays in the existing bucket `terraform-lab5-gemini-dev1`.

2. **Documents.** Twelve credit policies are already rendered from `corpus/` into `data/credit-policies/`. `docgen generate application` writes sample applications under `data/client-applications/`. `gmake deploy` runs `docgen upload`, which stores both prefixes in the bucket. The index step imports them.

3. **Index (`gmake index wait=1`).** The command ensures one Agent Search data store, `kb-credit-policies`, in location `global`. It imports `gs://lab5-gemini-dev1-credit-docs/credit-policies/` and `gs://lab5-gemini-dev1-credit-docs/client-applications/`. It then polls until indexed counts meet the floors: at least 12 policies and at least 3 applications.

4. **Agent Runtime.** The Google Agent Development Kit command `adk` deploys `agents/credit_officer/` to Agent Runtime in `us-east1`. InteractiveAgent calls RetrievalAgent. RetrievalAgent searches that one data store. The agent reads `DATA_STORE` from the environment. The value is `projects/lab5-gemini-dev1/locations/global/collections/default_collection/dataStores/kb-credit-policies`. Deploy prints a reasoning engine id. That id is `REASONING_ENGINE`. `adk run` and `adk web` stay local.

5. **Google Chat.** A credit officer sends a `MESSAGE`. `python -m docgen.google_chat` receives it, calls `reasoningEngines/{id}:streamQuery` with class method `async_stream_query`, and posts the model text in that thread. `user_id` is the Chat user. `session_id` is the space plus the thread. The handler holds no policy text.

```bash
gmake terraform-apply
uv run docgen generate application --all --local-only
gmake deploy
gmake index wait=1
export DATA_STORE=projects/lab5-gemini-dev1/locations/global/collections/default_collection/dataStores/kb-credit-policies
uv run adk deploy agent_engine --project=lab5-gemini-dev1 --region=us-east1 --display_name=credit-officer agents/credit_officer
export REASONING_ENGINE=<reasoning engine id>
uv run python -m docgen.google_chat
```
