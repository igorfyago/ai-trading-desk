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
import time

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


def _new_turn() -> dict:
    return {"user": "", "agent": "", "user_at": 0.0, "first_word_at": 0.0,
            "tools": [], "covered": None, "barge_in": False, "speaking": False}


def _flush_turn(turn: dict, call_id: str, rev: str, log_turn) -> None:
    """One logical turn, once it has actually been answered."""
    if not turn["user"] and not turn["agent"]:
        return
    dead = None
    if turn["user_at"] and turn["first_word_at"]:
        dead = int((turn["first_word_at"] - turn["user_at"]) * 1000)
    log_turn({"surface": "phone", "session": call_id, "persona": PHONE_PERSONA,
              "rev": rev, "user": turn["user"], "agent": turn["agent"],
              "ms_dead_air": dead, "tools": turn["tools"],
              "covered": turn["covered"], "barge_in": turn["barge_in"]})


def run_call_loop(call_id: str) -> None:
    """Attach to the accepted call and execute Riley's tools until hangup."""
    import websocket  # websocket-client

    from personas import PERSONAS, run_tool

    from common.calllog import log_turn, persona_rev

    p = PERSONAS[PHONE_PERSONA]
    rev = persona_rev(p["instructions"])
    ws = websocket.create_connection(
        f"wss://api.openai.com/v1/realtime?call_id={call_id}",
        header=[f"Authorization: Bearer {os.environ['OPENAI_API_KEY']}"],
        timeout=310,
    )
    turn = _new_turn()
    try:
        # the SAME audio config the browser gets, not a second hand-rolled one.
        # This line had drifted: it was missing interrupt_response False, so on
        # the phone the server truncated Marcus mid-word whenever its VAD heard
        # a breath or a chair, and it was missing transcription entirely, so the
        # caller's words were never transcribed and every phone turn logged with
        # no question attached to the answer.
        from web.server import AUDIO_CONFIG   # app runs as web.server:app

        ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime",
            "tools": p["tools"], "tool_choice": "auto",
            "audio": AUDIO_CONFIG(p["voice"]),
        }}))
        ws.send(json.dumps({"type": "response.create"}))   # Riley answers the phone

        while True:
            event = json.loads(ws.recv())
            etype = event.get("type")

            # the log tap: this socket already carries every event, it was just
            # throwing them away. Phone is the surface that matters most for
            # dead air, because a caller with no screen reads silence as a
            # dropped line.
            if etype == "input_audio_buffer.speech_started" and turn["speaking"]:
                turn["barge_in"] = True
            elif etype == "conversation.item.input_audio_transcription.completed":
                turn["user"] = (event.get("transcript") or "").strip()
                turn["user_at"] = time.time()
            elif etype == "response.output_audio_transcript.delta":
                turn["speaking"] = True
                if not turn["first_word_at"]:
                    turn["first_word_at"] = time.time()
                turn["agent"] += event.get("delta") or ""

            if etype == "response.done":
                turn["speaking"] = False
                calls = [i for i in (event.get("response", {}).get("output") or [])
                         if i.get("type") == "function_call"]
                for item in calls:
                    if turn["covered"] is None:
                        turn["covered"] = bool(turn["first_word_at"])
                    t0 = time.time()
                    output = run_tool(PHONE_PERSONA, item["name"],
                                      json.loads(item.get("arguments") or "{}"))
                    turn["tools"].append({"name": item["name"],
                                          "ms": int((time.time() - t0) * 1000)})
                    ws.send(json.dumps({"type": "conversation.item.create", "item": {
                        "type": "function_call_output",
                        "call_id": item["call_id"], "output": output}}))
                    ws.send(json.dumps({"type": "response.create"}))
                if not calls:
                    _flush_turn(turn, call_id, rev, log_turn)   # answer landed
                    turn = _new_turn()
    except Exception:
        pass  # caller hung up / socket closed — the call is simply over
    finally:
        _flush_turn(turn, call_id, rev, log_turn)  # a hangup mid-turn is evidence too
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
