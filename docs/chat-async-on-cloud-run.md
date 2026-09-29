# Asynchronous replies on Cloud Run

Research date: 2026-09-27. This report applies Google's HTTP-service and multiple-response patterns to Credit Policy. It is a research note, not a change to the running handler.

Sources for the Chat rules: [Choose a Google Chat app architecture](https://developers.google.com/workspace/chat/structure) (2026-09-03), [Receive and respond to interaction events](https://developers.google.com/workspace/chat/receive-respond-interactions) (2026-09-03), [spaces.messages.create](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/create) (2026-08-25), [Authenticate as a Google Chat app](https://developers.google.com/workspace/chat/authenticate-authorize-chat-app) (2026-04-20).

Sources for the host: [Cloud Run billing settings](https://cloud.google.com/run/docs/configuring/billing-settings), [General development tips](https://cloud.google.com/run/docs/tips/general), [Cloud Tasks](https://cloud.google.com/tasks/docs/dual-overview).

## What has to change, and what stays

Google's recommended host for this app stays Cloud Run (or App Engine) behind an HTTP endpoint. Replacing Cloud Run with Pub/Sub, Apps Script, Dialogflow, or a webhook changes the connection type. It does not raise the 30-second synchronous window, and it does not run `streamQuery`.

The conversation pattern changes from call-and-response to multiple responses:

1. Chat calls `https://credit-policy.ai.lab5.ca` exactly as it does now. The audience, the domain mapping, and the `chat@system.gserviceaccount.com` invoker binding stay.
2. `chat/main.py` accepts the `MESSAGE`, records the work, and returns one short message inside the 30-second window. "Looking up that application." is enough. That return is the entire synchronous reply.
3. A later call to `spaces.messages.create`, authenticated as the app with `https://www.googleapis.com/auth/chat.bot`, posts the judgement in the same thread. On failure, the same call posts the error, so the thread does not stay on the acknowledgement forever.

The HTTP handler in this repo returns one JSON object and does not call the Chat API. The spec says the status reply is that HTTP body and leaves `chat.googleapis.com` disabled. Both sentences have to move before this pattern can be built.

## Why the acknowledgement cannot share the officer call

`chat/main.py` holds the Cloud Run request until `streamQuery` returns, then writes the model text. Chat reads that body once. There is no supported way to flush a "processing" message on the same response and append the judgement after the model finishes.

On Sunday, 2026-09-27 (Eastern), four Google Chat requests reached the service. All four were HTTP 200. The two misses returned in 13.6 seconds and 21.8 seconds and were shown. The application-id turn returned in 43.8 seconds with a missing-data judgement, and Chat showed "Credit Policy not responding". The same thread was delivered again and finished in 25.8 seconds with the same judgement. Chat still showed the timeout bubble. An acknowledgement inside the first second stops that timeout. The officer call then has the Cloud Run limit, which is 300 seconds, and the handler's own `streamQuery` budget, which is 180 seconds.

## Where the officer call runs

The acknowledgement has to be a finished HTTP response. Anything the process does after that response is background work.

Cloud Run's default is request-based billing. CPU is allocated while the instance is handling a request, and Google's tip guide says that after the response is sent the instance's CPU is disabled or severely limited. Google's own guidance is to finish asynchronous work before delivering the response when request-based billing is on. Instance-based billing (formerly "CPU always allocated") keeps CPU for the life of the instance and is the setting Google documents for short background tasks. It still has no retry if the instance stops, and a failure after the response is invisible to Chat.

The reliable place for the officer call is a new request. [Cloud Tasks](https://cloud.google.com/tasks/docs/dual-overview) is the small addition that creates one. The Chat handler enqueues a task and only then returns the acknowledgement. The task calls the same Cloud Run service on a separate path, with an identity token for a service account that has `roles/run.invoker`. That path runs `streamQuery` and posts the judgement. CPU throttling can stay at the default, because the slow work is inside a request again. Cloud Tasks retries a failed HTTP task. A thread started after the Chat response does not.

The task URL should not be the Chat route. Chat's caller is `chat@system.gserviceaccount.com`. The task's caller is the tasks service account. The worker rejects the other identity. Granting that service account `run.invoker` does not require `allUsers`.

Enqueue, then acknowledge. If the enqueue fails, return an error so Chat still has a failed delivery to retry. If the enqueue succeeds and the HTTP response is lost, a second delivery must not start a second officer turn.

## One turn, even when Chat delivers the event twice

Sunday's application-id question arrived twice on thread `threads-bi5uwv4edii` because the first response missed 30 seconds. The pattern above removes that cause, and a lost response can still duplicate the event.

Use the incoming message resource name as the Cloud Tasks task id. A second enqueue of the same id is "already exists", and the handler acknowledges again without a second task. Use the same value, folded into Chat's `messageId` rules, as the create idempotency key. `messageId` must start with `client-`, be at most 63 characters, and contain only lowercase letters, numbers, and hyphens. `requestId` is the other idempotency field: the same ID and the same credentials return the message already created and do not change its text. Pick one and send it on every retry of the worker.

Pass the original user text, the Chat user resource name, the space, the thread, and the same `session_id` the handler already derives. The acknowledgement is the app's own message. It is not a new user turn for the officer.

## Two bubbles, or one bubble that changes

**Two messages.** The HTTP body is "Looking up that application." The worker creates a second message in the thread with the judgement. This needs no Chat API call on the request path, so a Chat API failure cannot block the acknowledgement. The first bubble stays on screen after the judgement arrives. If the worker dies, it should create an error message in that thread. Otherwise the acknowledgement is the last thing the officer sees.

**One message that updates.** The HTTP body is `{}`, which still counts as a response inside the window. The worker creates "Looking up that application." through the API, keeps the returned `name` (or the `messageId` it assigned), runs the officer, and patches that message to the judgement. [Update a message](https://developers.google.com/workspace/chat/update-messages) allows text and cards when the caller is the app. The synchronous HTTP body cannot be patched, because Chat does not return its resource name to the handler.

The two-message form is the smaller build. The patch form is the tighter thread. Both are the multiple-response pattern. Both keep Cloud Run.

## What this does not fix

The follow-up removes Chat's deadline. It does not shorten the officer. On the Sunday application-id turn the slow part was a new context cache plus a judgement hop of about 2,800 to 3,000 reasoning tokens, and the whole request was 43.8 seconds. After this pattern, that turn would show the acknowledgement immediately and the judgement about 45 seconds later. A miss that already fits in 30 seconds would take the same path, so every question would show the acknowledgement first.

`chat.bot` posts as Credit Policy into spaces where the app is a member. It does not need the `chat.app.*` administrator approval, and it does not need a user OAuth client. Enabling the API and allowing `credit-policy-agent` to call it is the permission change. The per-space write limit is one `spaces.messages.create` or `patch` per second, shared by every app in the space. A single acknowledgement plus one judgement fits. A retried create in the same second has to be the same `requestId` or `messageId`.

See also [Chat app architectures](chat-app-architectures.md) and [Chat messaging patterns](chat-messaging-patterns.md).
