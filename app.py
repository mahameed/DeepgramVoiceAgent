import base64
import json
import os
import sys
from queue import Queue
from threading import Lock, Thread, current_thread

from dotenv import load_dotenv
from flask import Flask
from flask_sock import Sock
from simple_websocket import Client, ConnectionClosed

from pharmacy_functions import FUNCTION_MAP

# Ensure stdout and stderr flush immediately in containers and daemon threads
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

load_dotenv()

app = Flask(__name__)
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(app)

BUFFER_SIZE = 20 * 160


@app.route("/health")
def health():
    print("Health check requested")
    return {"status": "ok"}


def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise Exception("DEEPGRAM_API_KEY not found")

    return Client.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key],
    )


def load_config():
    with open("config.json", "r") as f:
        return json.load(f)


def sts_send(session, data):
    with session["send_lock"]:
        session["sts_ws"].send(data)


def twilio_send(session, message):
    with session["twilio_send_lock"]:
        session["twilio_ws"].send(json.dumps(message))


def handle_barge_in(decoded, session):
    if decoded["type"] == "UserStartedSpeaking":
        streamsid = session["streamsid"]
        if not streamsid:
            return

        clear_message = {
            "event": "clear",
            "streamSid": streamsid,
        }
        twilio_send(session, clear_message)


def execute_function_call(func_name, arguments):
    if func_name in FUNCTION_MAP:
        result = FUNCTION_MAP[func_name](**arguments)
        print(f"Function call result: {result}")
        return result

    result = {"error": f"Unknown function: {func_name}"}
    print(result)
    return result


def create_function_call_response(func_id, func_name, result):
    return {
        "type": "FunctionCallResponse",
        "id": func_id,
        "name": func_name,
        "content": json.dumps(result),
    }


def handle_function_call_request(decoded, session):
    try:
        for function_call in decoded["functions"]:
            func_name = function_call["name"]
            func_id = function_call["id"]
            arguments = json.loads(function_call["arguments"])

            print(f"Function call: {func_name} (ID: {func_id}), arguments: {arguments}")

            result = execute_function_call(func_name, arguments)
            function_result = create_function_call_response(func_id, func_name, result)
            sts_send(session, json.dumps(function_result))
            print(f"Sent function result: {function_result}")

    except Exception as e:
        print(f"Error calling function: {e}")
        error_result = create_function_call_response(
            func_id if "func_id" in locals() else "unknown",
            func_name if "func_name" in locals() else "unknown",
            {"error": f"Function call failed with: {str(e)}"},
        )
        sts_send(session, json.dumps(error_result))


def handle_text_message(decoded, session):
    handle_barge_in(decoded, session)

    if decoded["type"] == "FunctionCallRequest":
        handle_function_call_request(decoded, session)


def sts_sender(session):
    print("sts_sender started")
    while True:
        chunk = session["audio_queue"].get()
        if chunk is None or session["closed"]:
            break
        try:
            sts_send(session, chunk)
        except Exception as e:
            print(f"sts_sender error: {e}")
            break


def sts_receiver(session):
    print("sts_receiver started")
    streamsid = session["streamsid_queue"].get()
    if not streamsid or session["closed"]:
        return

    while not session["closed"]:
        try:
            message = session["sts_ws"].receive(timeout=1)
        except (ConnectionClosed, OSError) as e:
            print(f"sts_receiver error: {e}")
            break

        if message is None:
            continue

        if isinstance(message, str):
            print(message)
            decoded = json.loads(message)
            handle_text_message(decoded, session)
            continue

        media_message = {
            "event": "media",
            "streamSid": streamsid,
            "media": {"payload": base64.b64encode(message).decode("ascii")},
        }
        try:
            twilio_send(session, media_message)
        except (ConnectionClosed, OSError) as e:
            print(f"Twilio send error: {e}")
            break


def start_session(twilio_ws):
    session = {
        "sts_ws": sts_connect(),
        "send_lock": Lock(),
        "twilio_ws": twilio_ws,
        "twilio_send_lock": Lock(),
        "audio_queue": Queue(),
        "streamsid_queue": Queue(),
        "streamsid": None,
        "inbuffer": bytearray(b""),
        "closed": False,
        "threads": [],
    }
    sts_send(session, json.dumps(load_config()))

    session["threads"] = [
        Thread(target=sts_sender, args=(session,), daemon=True),
        Thread(target=sts_receiver, args=(session,), daemon=True),
    ]
    for thread in session["threads"]:
        thread.start()

    return session


def stop_session(session):
    if not session or session["closed"]:
        return

    session["closed"] = True
    session["audio_queue"].put(None)
    if session["streamsid"] is None:
        session["streamsid_queue"].put(None)

    try:
        session["sts_ws"].close()
    except Exception:
        pass

    for thread in session["threads"]:
        if thread is not current_thread():
            thread.join(timeout=2)


def handle_client_event(session, data):
    event = data.get("event")

    if event == "start":
        print("Received Twilio stream start")
        streamsid = data["start"]["streamSid"]
        session["streamsid"] = streamsid
        session["streamsid_queue"].put(streamsid)
    elif event == "connected":
        return True
    elif event == "media":
        media = data["media"]
        chunk = base64.b64decode(media["payload"])
        if media.get("track", "inbound") == "inbound":
            session["inbuffer"].extend(chunk)
        while len(session["inbuffer"]) >= BUFFER_SIZE:
            buffered = bytes(session["inbuffer"][:BUFFER_SIZE])
            session["audio_queue"].put(buffered)
            session["inbuffer"] = session["inbuffer"][BUFFER_SIZE:]
    elif event == "stop":
        return False

    return True


@sock.route("/media")
def media(twilio_ws):
    print("Twilio Media Stream connected")
    session = None
    try:
        session = start_session(twilio_ws)
        while not session["closed"]:
            message = twilio_ws.receive()
            if message is None:
                break
            if isinstance(message, bytes):
                message = message.decode("utf-8")

            data = json.loads(message)
            if data.get("event") != "media":
                print(f"Received Twilio event: {data.get('event')}")
            if not handle_client_event(session, data):
                break
    except ConnectionClosed:
        print("Twilio Media Stream disconnected")
    except Exception as e:
        print(f"Twilio Media Stream error: {e}")
    finally:
        stop_session(session)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", debug=True, port=port)
