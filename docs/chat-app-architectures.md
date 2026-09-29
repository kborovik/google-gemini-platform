# Google Chat app architectures

Research date: 2026-09-27. Primary source: [Choose a Google Chat app architecture](https://developers.google.com/workspace/chat/structure), last updated 2026-09-03.

Google documents seven ways to build a Chat app. The choice is the connection between Chat and the app, not the language or the model behind it. Two marks appear in Google's capability table. **Recommended** is the architecture Google points at for that capability. **Possible** means another architecture can do it, and Google still points somewhere else first. A blank cell means that architecture is not a fit.

Credit Policy is already the recommended shape for an organization app that talks to users and calls other Google APIs: an HTTP service on Cloud Run. The other six shapes solve a different problem.

## HTTP service

An HTTP service is the architecture Google recommends for a public Marketplace app, for an organization app, and for an app that both answers in the HTTP response and posts later through the Chat API. The app can be written in any language and deployed with ordinary CI. On Google Cloud the documented hosts are Cloud Run and App Engine. The quickstart builds a Cloud Run function: [Build an HTTP Google Chat app](https://developers.google.com/workspace/chat/quickstart/gcf-app).

The flow Google draws has six steps:

1. A person sends a message to the app in a direct message or a space.
2. Chat sends an HTTPS request to the app. The user agent is `Google-Dynamite`.
3. The app may call another system.
4. The app returns one HTTP response.
5. Chat shows that response to the person.
6. The app may later call the Chat API and post more messages.

Credit Policy uses this path today. Chat posts to `https://credit-policy.ai.lab5.ca`. Cloud Run service `chat` runs `chat/main.py`, calls the reasoning engine, and returns one JSON message. Step 6 is unused. The handler never calls the Chat API.

Two audience settings exist for that HTTPS request. [Verify requests from Google Chat](https://developers.google.com/workspace/chat/verify-requests-from-chat) (2026-09-03):

- **HTTP endpoint URL.** The bearer token is a Google-signed OIDC ID token. `email` is `chat@system.gserviceaccount.com`. `audience` is the endpoint URL, with no extra slash. On Cloud Run, IAM checks the token when that service account has `roles/run.invoker`. This is the setting this repo uses. Terraform sets `custom_audiences` to exactly `https://credit-policy.ai.lab5.ca`, and the only invoker binding is `chat@system.gserviceaccount.com`.
- **Project number.** The bearer token is a JWT signed by the same service account, and `audience` is the Cloud project number. Google recommends this only when the URL will change and the project number should stay the check.

A failed check should be HTTP 401. Cloud Run returns 403 when the caller lacks `run.invoker`.

## Pub/Sub

[Build a Google Chat app behind a firewall with Pub/Sub](https://developers.google.com/workspace/chat/quickstart/pub-sub) (2026-09-03) is the architecture Google recommends when Chat cannot open an HTTP connection to the app, and when the app subscribes to Chat space events through the [Google Workspace Events API](https://developers.google.com/workspace/chat/events-overview).

Chat publishes the interaction onto a topic. A server on either side of the firewall pulls or receives that message, then calls the Chat API if it has anything to say. There is no HTTP response body to carry the reply. Google's table marks synchronous send-and-receive as possible on Pub/Sub and recommended on an HTTP service. The publisher identity in the quickstart is `chat-api-push@system.gserviceaccount.com`. The sample posts the echo with app authentication and the scope `https://www.googleapis.com/auth/chat.bot`.

Pub/Sub delivery is at-least-once. The app has to treat a repeated event as the same turn. Switching Credit Policy's connection setting from HTTP to Pub/Sub would drop the custom audience, the single invoker binding, and the ability to answer inside the HTTP response. It would not remove the need for a server, and it would not make the reasoning-engine call faster.

## Incoming webhook

A webhook is a URL that posts into one space. [Build a Google Chat app as a webhook](https://developers.google.com/workspace/chat/quickstart/webhooks) (2026-09-03) is explicit: users cannot talk to a webhook, and a webhook cannot receive interaction events. It cannot be published to the Marketplace. Google recommends it for alerts into a single space. The per-space write quota for `spaces.messages.create` is shared with webhooks, and a webhook is also held to about one request per second in that space. The create response for a webhook fills `name` and `thread.name` only.

A webhook cannot host Credit Policy. Officers send questions and expect an answer in the same thread.

## Apps Script

Apps Script is the low-code host. The script receives the event and returns the reply. Google recommends it for a team or an organization app that also reads Sheets, Calendar, Drive, or similar Workspace services, and that wants Apps Script to hold the OAuth tokens. Google allows a public Apps Script Chat app and advises against it because of Apps Script daily quotas. The quickstart is [Build a Google Chat app with Google Apps Script](https://developers.google.com/workspace/chat/quickstart/apps-script-app).

Credit Policy's officer is a Python Agent Runtime service. Moving that call into Apps Script would replace the runtime, not the Chat timeout.

## AppSheet

AppSheet builds a domain-shared Chat app without code, from templates. Google recommends it for the author and their team. Some AppSheet web-app features are absent in the Chat surface. It is not a host for a custom reasoning-engine client.

## Dialogflow

Dialogflow ES and Dialogflow CX are Google's natural-language agents for Chat. The integration is synchronous: Dialogflow receives the message, optionally calls a webhook, and returns one reply. Google recommends Dialogflow when the product being built is that virtual agent. Credit Policy already has its own officer on Agent Runtime. Dialogflow would be a second agent, not a transport for the one that exists.

## Command-line script

A script calls the Chat API and never receives a person clicking or typing at the app. Google recommends it for one-way automation. It cannot be published to the Marketplace. It does not apply to an officer that officers message.

## What this means for Credit Policy

| Need | Architecture Google recommends |
| --- | --- |
| Officer types a question and the app answers | HTTP service |
| Answer now, and post again after a long job | HTTP service, then the Chat API |
| Chat cannot reach the server | Pub/Sub |
| Notice when a space changes, without a person messaging the app | Pub/Sub plus the Workspace Events API |
| One-way alert into one space | Incoming webhook |
| Spreadsheet-shaped app for one team | Apps Script or AppSheet |

Keep the HTTP Cloud Run service. The patterns below describe what that service is allowed to say, and when it has to call the Chat API instead.

See also [Chat messaging patterns](chat-messaging-patterns.md) and [Asynchronous replies on Cloud Run](chat-async-on-cloud-run.md).
