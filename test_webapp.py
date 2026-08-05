import threading

from fastapi.testclient import TestClient

import webapp


# ------------------------------------------------------------------ level 1 ---
# Bridge-only tests: no HTTP/WebSocket at all, just the worker-thread hooks
# and the shared queue.Queue, exactly like the live smoke tests already run
# against gui.py's _ask_fn/_confirm_fn.

def test_bridge_log_puts_event_on_queue():
    bridge = webapp.AutomationBridge()
    bridge._log("hello")
    assert bridge.events.get_nowait() == ("log", "hello")


def test_bridge_confirm_fn_blocks_until_response():
    bridge = webapp.AutomationBridge()
    done = threading.Event()

    def worker():
        bridge._confirm_fn("please confirm")
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    kind, prompt, response = bridge.events.get(timeout=2)
    assert kind == "confirm"
    assert prompt == "please confirm"
    assert not done.is_set()  # still blocked

    response.put(None)
    t.join(timeout=2)
    assert done.is_set()


def test_bridge_ask_fn_blocks_and_returns_answer():
    bridge = webapp.AutomationBridge()
    result = {}

    def worker():
        result["answer"] = bridge._ask_fn({"label": "Pick one", "type": "text", "options": []})

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    kind, item, response = bridge.events.get(timeout=2)
    assert kind == "ask"
    assert item["label"] == "Pick one"

    response.put("my answer")
    t.join(timeout=2)
    assert result["answer"] == "my answer"


def test_bridge_on_frame_encodes_base64():
    bridge = webapp.AutomationBridge()
    bridge._on_frame(b"fake-jpeg-bytes")
    kind, data = bridge.events.get_nowait()
    assert kind == "frame"
    import base64
    assert base64.b64decode(data) == b"fake-jpeg-bytes"


def test_bridge_start_returns_false_when_already_running():
    bridge = webapp.AutomationBridge()
    bridge.worker_thread = threading.Thread(target=lambda: threading.Event().wait())
    bridge.worker_thread.daemon = True
    bridge.worker_thread.start()
    try:
        assert bridge.start() is False
    finally:
        bridge.worker_thread = None  # let the thread just die with the process


# ------------------------------------------------------------------ level 2 ---
# FastAPI TestClient: drives a fake run_automation_fn through /start, /ws, /stop.

async def fake_automation(profile, log=print, confirm_fn=None, ask_fn=None, on_frame=None,
                           num_listings=5, max_steps=6):
    log("starting")
    if on_frame:
        on_frame(b"fake-frame-bytes")
    confirm_fn("please confirm")
    log("confirmed")
    answer = ask_fn({"label": "Pick one", "reasoning": "test", "type": "text", "options": []})
    log(f"got answer: {answer}")


def test_start_streams_log_confirm_ask_finished(monkeypatch):
    monkeypatch.setattr(webapp, "load_profile", lambda: {})
    app = webapp.create_app(run_automation_fn=fake_automation)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            state = ws.receive_json()
            assert state["type"] == "state"
            assert state["running"] is False

            resp = client.post("/start")
            assert resp.status_code == 200

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "starting"}

            msg = ws.receive_json()
            assert msg["type"] == "frame"

            msg = ws.receive_json()
            assert msg == {"type": "confirm", "prompt": "please confirm"}

            ws.send_json({"type": "confirm_response"})

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "confirmed"}

            msg = ws.receive_json()
            assert msg["type"] == "ask"
            assert msg["item"]["label"] == "Pick one"

            ws.send_json({"type": "ask_response", "answer": "my answer"})

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "got answer: my answer"}

            msg = ws.receive_json()
            assert msg == {"type": "finished", "text": "Run finished."}


def test_start_twice_returns_409(monkeypatch):
    monkeypatch.setattr(webapp, "load_profile", lambda: {})

    async def slow_automation(profile, log=print, confirm_fn=None, ask_fn=None, on_frame=None,
                               num_listings=5, max_steps=6):
        confirm_fn("blocking forever until stopped")

    app = webapp.create_app(run_automation_fn=slow_automation)
    with TestClient(app) as client:
        resp1 = client.post("/start")
        assert resp1.status_code == 200
        resp2 = client.post("/start")
        assert resp2.status_code == 409
        client.post("/stop")


def test_stop_when_not_running_returns_400(monkeypatch):
    monkeypatch.setattr(webapp, "load_profile", lambda: {})
    app = webapp.create_app(run_automation_fn=fake_automation)
    with TestClient(app) as client:
        resp = client.post("/stop")
        assert resp.status_code == 400


def test_reconnect_mid_pending_ask_replays_state(monkeypatch):
    monkeypatch.setattr(webapp, "load_profile", lambda: {})
    app = webapp.create_app(run_automation_fn=fake_automation)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws1:
            ws1.receive_json()  # initial state
            client.post("/start")
            ws1.receive_json()  # log "starting"
            ws1.receive_json()  # frame
            ws1.receive_json()  # confirm
            ws1.send_json({"type": "confirm_response"})
            ws1.receive_json()  # log "confirmed"
            ask_msg = ws1.receive_json()
            assert ask_msg["type"] == "ask"
        # ws1 disconnects here WITHOUT answering the ask -- the worker thread
        # is still blocked on it; bridge-instance state must remember that.

        with client.websocket_connect("/ws") as ws2:
            state = ws2.receive_json()
            assert state["type"] == "state"
            assert state["pending"]["kind"] == "ask"
            assert state["pending"]["item"]["label"] == "Pick one"
            assert "starting" in state["log_tail"]

            ws2.send_json({"type": "ask_response", "answer": "answered after reconnect"})
            msg = ws2.receive_json()
            assert msg == {"type": "log", "text": "got answer: answered after reconnect"}
            msg = ws2.receive_json()
            assert msg == {"type": "finished", "text": "Run finished."}
