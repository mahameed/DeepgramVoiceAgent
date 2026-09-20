# MUST BE THE FIRST TWO LINES IN THE FILE
import eventlet
eventlet.monkey_patch()

import base64
import json
import os
from queue import Queue

from dotenv import load_dotenv
from flask import Flask, request
from flask_socketio import SocketIO, emit
from simple_websocket import Client, ConnectionClosed

from pharmacy_functions import FUNCTION_MAP

load_dotenv()

app = Flask(__name__)

# logger=True and engineio_logger=True force errors out to the terminal
socketio = SocketIO(app, cors_allowed_origins="*", logger=True, engineio_logger=True)

BUFFER_SIZE = 20 * 160
sessions = {}


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


def handle_barge_in(decoded, sid):
    if decoded["type"] == "UserStartedSpeaking":
        session = sessions.get(sid)
        clear_message = {
            "event": "clear",
            "streamSid": session["streamsid"] if session else None,
        }
        socketio.emit("clear", clear_message, to=sid)


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


def handle_text_message(decoded, sid, session):
    handle_barge_in(decoded, sid)

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


def sts_receiver(sid, session):
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
            handle_text_message(decoded, sid, session)
            continue

        media_message = {
            "event": "media",
            "streamSid": streamsid,
            "media": {"payload": base64.b64encode(message).decode("ascii")},
        }
        socketio.emit("media", media_message, to=sid)


def start_session(sid):
    session = {
        "sts_ws": sts_connect(),
        "send_lock": eventlet.semaphore.Semaphore(1),
        "audio_queue": Queue(),
        "streamsid_queue": Queue(),
        "streamsid": None,
        "inbuffer": bytearray(b""),
        "closed": False,
    }
    sessions[sid] = session
    sts_send(session, json.dumps(load_config()))

    socketio.start_background_task(sts_sender, session)
    socketio.start_background_task(sts_receiver, sid, session)
    return session


def stop_session(sid):
    session = sessions.pop(sid, None)
    if not session:
        return

    session["closed"] = True
    session["audio_queue"].put(None)
    if session["streamsid"] is None:
        session["streamsid_queue"].put(None)

    try:
        session["sts_ws"].close()
    except Exception:
        pass


def handle_client_event(sid, data):
    session = sessions.get(sid)
    if not session:
        return

    event = data.get("event")

    if event == "start":
        print("get our streamsid")
        streamsid = data["start"]["streamSid"]
        session["streamsid"] = streamsid
        session["streamsid_queue"].put(streamsid)
    elif event == "connected":
        return
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
        stop_session(sid)


@socketio.on("connect")
def handle_connect():
    print(f"!!! SERVER LOG: Client connected! SID: {request.sid}")
    try:
        start_session(request.sid)
        emit("server_response", {"message": "Connected to local WebSocket!"})
    except Exception as e:
        print(f"Failed to start session: {e}")
        emit("server_response", {"error": str(e)})
        return False


@socketio.on("disconnect")
def handle_disconnect():
    print(f"!!! SERVER LOG: Client disconnected! SID: {request.sid}")
    stop_session(request.sid)


@socketio.on("client_message")
def handle_message(data):
    if isinstance(data, str):
        data = json.loads(data)

    event = data.get("event") if isinstance(data, dict) else None
    if event != "media":
        print(f"!!! SERVER LOG: Received data: {data}")

    handle_client_event(request.sid, data)


if __name__ == "__main__":
    socketio.run(app, debug=True, port=5000)
