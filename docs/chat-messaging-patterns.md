# Google Chat messaging patterns

Research date: 2026-09-27. Primary sources: [Choose a Google Chat app architecture](https://developers.google.com/workspace/chat/structure) (2026-09-03) and [Receive and respond to interaction events](https://developers.google.com/workspace/chat/receive-respond-interactions) (2026-09-03).

Google separates the connection (HTTP, Pub/Sub, Apps Script) from the conversation. The same HTTP app can use any of the patterns below. The pattern decides whether the reply is the HTTP body, a later Chat API call, or both.

## Call and response

One user message produces one app message. The app returns a [Message](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages) in the HTTP body. Chat posts that object in the space where the event happened. A synchronous reply does not need a Chat API credential.

Google's rule for that body: the app must respond within 30 seconds. The guides do not document a connection setting, a quota, or a Cloud Run timeout that changes those 30 seconds. The alternative they document is an asynchronous call to the Chat API.

The HTTP body is one message. Chat does not treat a streamed or chunked body as a first bubble followed by a second one. `text`, `cardsV2`, and `accessoryWidgets` ride in that single object. The maximum size of a message, including contents, is 32,000 bytes ([spaces.messages.create](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/create), 2026-08-25).

Credit Policy uses this pattern. `chat/main.py` waits for `streamQuery` and returns `{text, thread}`. On Sunday, 2026-09-27, the two name lookups finished in 13.6 seconds and 21.8 seconds and Chat showed them. The application-id lookup finished in 43.8 seconds with HTTP 200. Chat had already shown "Credit Policy not responding" and did not display the judgement. The Cloud Run service timeout is 300 seconds. That timer keeps the handler process alive. It does not extend Chat's window.

## Multiple responses

Google's multiple-response pattern is the one that covers a long officer turn. The diagram in the architecture guide is:

1. The user sends a message ("Monitor traffic").
2. The app returns a synchronous acknowledgement ("Monitoring on").
3. Later, the app posts one or more further messages by calling the REST API ("New traffic").
4. The user can send another message, and the app acknowledges that one the same way.

The architectures Google recommends for this pattern are an HTTP service, Pub/Sub, Apps Script, and AppSheet. Dialogflow is recommended for a single synchronous reply, not for this follow-up.

The synchronous half is still the 30-second HTTP response. The later half is [spaces.messages.create](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/create). Returning "Looking up that application." from `chat/main.py` would satisfy step 2. The judgement has to be step 3. Holding the HTTP request open until the judgement is ready, then writing both sentences into that one body, is still call-and-response. Chat waits for the body and gives up at 30 seconds.

A synchronous response does not hand back the resource name of the message Chat created from it. Updating that bubble later requires the app to have created the message itself through the API, which returns `name`. [Update a message](https://developers.google.com/workspace/chat/update-messages) (2026-09-17) with app authentication can change both `text` and `cardsV2`. With user authentication it can change `text` only.

## Events, one-way posts, and dialogs

Three more patterns show up in the same guide. They are real Chat designs and they are the wrong tool for an officer question.

**Query or subscribe.** The app learns that a space changed by calling the Chat API or by holding a Workspace Events API subscription. The event arrives on a Pub/Sub topic. The app may then post with the Chat API. Nobody has to message the app first. That is how an app welcomes a new member. It is not how an officer asks for a filing.

**One-way from the app.** The app posts an alert through the Chat API or a webhook. Users do not converse with it. Google recommends a webhook, an HTTP service, Apps Script, AppSheet, or a script.

**One-way to the app.** The user messages the app and the app processes the event without answering. Google says this is a poor experience and discourages it. An HTTP `{}` with no later post is this pattern. An HTTP `{}` that is only the acknowledgement, followed by a Chat API message, is multiple responses.

**Dialogs.** A card dialog is a series of interaction events. Each button or form submit is a new event, and each one still needs its own response inside the same 30-second window. Dialogs collect input. They do not lengthen the time available for `streamQuery`.

**Commands and link previews.** Slash commands, quick commands, and link previews are separate triggers. Credit Policy leaves them off. Each would be another event type on the same endpoint, with the same response rules.

## How a later message is authenticated

[Authenticate and authorize Chat apps](https://developers.google.com/workspace/chat/authenticate-authorize) (2026-09-18) defines two callers.

**App authentication** uses a service account in the same Cloud project as the Chat app and the scope `https://www.googleapis.com/auth/chat.bot`. The app acts as itself. Chat shows the app as the sender. The message may contain text, cards, and accessory widgets. [Authenticate as a Google Chat app](https://developers.google.com/workspace/chat/authenticate-authorize-chat-app) (2026-04-20) says `chat.bot` needs no administrator approval and no user consent. The app can self-grant that scope. Scopes that begin with `https://www.googleapis.com/auth/chat.app.*` do need a one-time administrator approval. `chat.bot` cannot be used with user credentials or with domain-wide delegation.

**User authentication** uses an OAuth consent screen and a user scope such as `chat.messages`. Chat shows the user as the sender. The body can contain text only. The first action on a user's behalf requires that user's consent, unless an administrator installed the app or granted domain-wide delegation.

A judgement posted back into the officer's thread should use app authentication. The sender should be Credit Policy, and the body may need more than plain text. `credit-policy-agent` is already the Cloud Run runtime service account in this project. With `chat.googleapis.com` enabled, its application-default credentials can mint a `chat.bot` token. No separate OAuth client is required for that scope. This repository's spec currently leaves `chat.googleapis.com` disabled, so that call is a spec change before it is a code change.

`spaces.messages.create` details that matter for a follow-up:

- Pass the space as `parent` (`spaces/{space}`) and set `thread.name` from the event so the judgement lands in the same thread. `messageReplyOption` is ignored when the call is the response to a user interaction. Inside a thread, Chat places the reply in that thread. In a direct message, threading options are limited. The event's thread name is the value to send.
- `requestId` makes the create idempotent. The same ID and the same credentials return the existing message. A later attempt with that ID does not edit the text.
- `messageId` is a client name for the message. It must start with `client-`, contain at most 63 characters, and use only lowercase letters, numbers, and hyphens. It must be unique in the space. The resource can then be addressed as `spaces/{space}/messages/client-...` for a later patch, without storing Chat's assigned ID.
- App authentication can patch a message the app created. It cannot patch a message that exists only because Chat turned the HTTP response into a bubble.

## Quotas on the follow-up

[Usage limits](https://developers.google.com/workspace/chat/limits) (2026-09-03):

- Per project, `spaces.messages.create`, `patch`, and `delete` share 3,000 writes per minute.
- Per space, writes including `spaces.messages.create` and `patch` share 1 write per second, across every app in that space. Webhooks count against the same space.
- Over the limit, the API returns HTTP 429. The documented response is truncated exponential backoff.

One acknowledgement plus one judgement in a direct message is far under the project quota. The per-space limit of one write per second matters if a retry fires the create twice in the same second. `requestId` or `messageId` makes the second create the same message.

## What Credit Policy does today

The handler implements call-and-response only. A turn that finishes inside 30 seconds appears in the thread. A turn that finishes later is dropped by Chat even when Cloud Run returns HTTP 200. The next report is the HTTP-service version of multiple responses, hosted on the current Cloud Run service.

See also [Chat app architectures](chat-app-architectures.md) and [Asynchronous replies on Cloud Run](chat-async-on-cloud-run.md).
