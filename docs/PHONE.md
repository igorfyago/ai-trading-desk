# Giving the desk a real phone number (OpenAI Realtime SIP)

Callers dial a normal phone number; Riley answers, books appointments, and reads the desk — same persona, same server-side tools as the web app. The code is already deployed; three one-time setup steps remain (all yours, ~15 minutes):

## 1. OpenAI webhook (platform.openai.com → Settings → Webhooks)

- Create endpoint: `https://desk.b4rruf3t.com/webhook/openai`
- Subscribe to event: `realtime.call.incoming`
- Copy the signing secret → store it:
  ```bash
  aws ssm put-parameter --name /desk/OPENAI_WEBHOOK_SECRET --type SecureString --value "whsec_..." --overwrite
  # then append to the server's deploy/.env and `docker compose up -d desk`
  ```

## 2. Note your OpenAI project id

platform.openai.com → Settings → General → `proj_...` — used in the SIP address below.

## 3. Twilio (or any SIP trunk provider)

1. Buy a local number (~$1.15/mo + ~$0.004–0.01/min).
2. Elastic SIP Trunking → create trunk → **Origination URI**:
   `sip:proj_YOURPROJECTID@sip.api.openai.com;transport=tls`
3. Attach the number to the trunk. Done — call it.

## How it works (already live in `web/phone.py`)

```
caller → Twilio number → SIP trunk → OpenAI Realtime
       → webhook realtime.call.incoming → POST /webhook/openai (signature-verified)
       → accept(call_id) with Riley's persona + phone addendum
       → background WebSocket: session.update (tools, semantic VAD low)
         → Riley answers first; every function_call executes server-side
         → hangup closes the loop
```

Costs per minute ≈ Twilio ~$0.01 + Realtime audio tokens (~$0.10–0.20 typical conversation) — set the OpenAI project spend cap accordingly.

Call transfer (`/refer` to a human number) and outbound calls are supported by the same API when we want them.
