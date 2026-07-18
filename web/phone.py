"""Phone calls into the desk: OpenAI Realtime SIP.

Flow (see docs/PHONE.md for the one-time Twilio/OpenAI console setup):
  caller dials the Twilio number
    -> Twilio Elastic SIP trunk -> sip:<project_id>@sip.api.openai.com
    -> OpenAI webhook `realtime.call.incoming` hits POST /webhook/openai
    -> we accept the call with Riley's persona
    -> a background thread attaches via WebSocket and runs her TOOLS
       server-side (bookings, desk reads) for the duration of the call.

Requires OPENAI_WEBHOOK_SECRET (from the OpenAI console webhook endpoint).
Everything degrades gracefully when unset — the desk just has no phone line.
"""

import json
import os
import threading

import httpx

PHONE_PERSONA = "marcus"   # you call the desk to reach the analyst, not a receptionist
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime-2.1")


def webhook_secret() -> str | None:
    return os.getenv("OPENAI_WEBHOOK_SECRET")


def verify_and_parse(body: bytes, headers) -> dict | None:
    """Verify the webhook signature via the openai SDK; None if invalid."""
    try:
        from openai import OpenAI

        client = OpenAI(webhook_secret=webhook_secret())
        event = client.webhooks.unwrap(body, dict(headers))
        return json.loads(event.model_dump_json()) if hasattr(event, "model_dump_json") else dict(event)
    except Exception:
        return None


def accept_call(call_id: str) -> bool:
    from personas import PERSONAS

    p = PERSONAS[PHONE_PERSONA]
    resp = httpx.post(
        f"https://api.openai.com/v1/realtime/calls/{call_id}/accept",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "type": "realtime",
            "model": REALTIME_MODEL,
            "instructions": p["instructions"] +
            "\n\n# Phone line\nThis is a real phone call: audio only, keep turns short, "
            "confirm contact details by reading them back digit by digit.",
            "voice": p["voice"],
        },
        timeout=15,
    )
    return resp.status_code == 200


def run_call_loop(call_id: str) -> None:
    """Attach to the accepted call and execute Riley's tools until hangup."""
    import websocket  # websocket-client

    from personas import PERSONAS, run_tool

    p = PERSONAS[PHONE_PERSONA]
    ws = websocket.create_connection(
        f"wss://api.openai.com/v1/realtime?call_id={call_id}",
        header=[f"Authorization: Bearer {os.environ['OPENAI_API_KEY']}"],
        timeout=310,
    )
    try:
        ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime",
            "tools": p["tools"], "tool_choice": "auto",
            "audio": {"input": {"turn_detection": {"type": "semantic_vad", "eagerness": "low"}}},
        }}))
        ws.send(json.dumps({"type": "response.create"}))   # Riley answers the phone

        while True:
            event = json.loads(ws.recv())
            if event.get("type") == "response.done":
                for item in (event.get("response", {}).get("output") or []):
                    if item.get("type") == "function_call":
                        output = run_tool(PHONE_PERSONA, item["name"],
                                          json.loads(item.get("arguments") or "{}"))
                        ws.send(json.dumps({"type": "conversation.item.create", "item": {
                            "type": "function_call_output",
                            "call_id": item["call_id"], "output": output}}))
                        ws.send(json.dumps({"type": "response.create"}))
    except Exception:
        pass  # caller hung up / socket closed — the call is simply over
    finally:
        try:
            ws.close()
        except Exception:
            pass


def handle_incoming(event: dict) -> dict:
    call_id = (event.get("data") or {}).get("call_id")
    if not call_id:
        return {"handled": False, "reason": "no call_id"}
    if not accept_call(call_id):
        return {"handled": False, "reason": "accept failed"}
    threading.Thread(target=run_call_loop, args=(call_id,), daemon=True).start()
    return {"handled": True, "call_id": call_id}
