import asyncio
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

    kind, prompt, response, retryable, allow_apply_current = bridge.events.get(timeout=2)
    assert kind == "confirm"
    assert prompt == "please confirm"
    assert retryable is False
    assert allow_apply_current is False
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
                           num_listings=5, max_steps=6, should_reset=None):
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
            assert msg == {
                "type": "confirm", "prompt": "please confirm", "retryable": False, "allow_apply_current": False,
            }

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
                               num_listings=5, max_steps=6, should_reset=None):
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


def test_reset_when_nothing_running_returns_error():
    # Soft reset only makes sense against a running automation -- same
    # semantics as /stop, unlike the old hard-reset's start-if-idle fallback.
    app = webapp.create_app(run_automation_fn=fake_automation)
    with TestClient(app) as client:
        resp = client.post("/reset")
        assert resp.status_code == 400


def test_reset_answers_pending_confirm_with_reset_sentinel(monkeypatch):
    # Soft reset must never tear down and relaunch the browser/session --
    # unlike the old hard reset, this proves it's still the SAME run (no
    # second "run starting" log, no "finished" event) that simply receives
    # a "reset" answer to whatever confirm() it was blocked on.
    monkeypatch.setattr(webapp, "load_profile", lambda: {})

    async def confirm_automation(profile, log=print, confirm_fn=None, ask_fn=None, on_frame=None,
                                  num_listings=5, max_steps=6, should_reset=None):
        log("run starting")
        action = confirm_fn("waiting for input")
        log(f"got action: {action}")

    app = webapp.create_app(run_automation_fn=confirm_automation)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # initial state
            client.post("/start")

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "run starting"}
            msg = ws.receive_json()
            assert msg == {
                "type": "confirm", "prompt": "waiting for input", "retryable": False, "allow_apply_current": False,
            }

            resp = client.post("/reset")
            assert resp.status_code == 200

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "got action: reset"}
            msg = ws.receive_json()
            assert msg["type"] == "finished"


def test_apply_current_action_reaches_worker_thread(monkeypatch):
    # The "Apply to Current Page" button skips jobright's listing pages
    # entirely (see main.run_automation's confirm_or_reset docs) -- proves
    # allow_apply_current makes it all the way from confirm_fn's call
    # through the WebSocket and back as the "apply_current" answer.
    monkeypatch.setattr(webapp, "load_profile", lambda: {})

    async def apply_current_automation(profile, log=print, confirm_fn=None, ask_fn=None, on_frame=None,
                                        num_listings=5, max_steps=6, should_reset=None):
        action = confirm_fn("Browse listings, or apply to whatever's open", allow_apply_current=True)
        log(f"got action: {action}")

    app = webapp.create_app(run_automation_fn=apply_current_automation)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # initial state
            client.post("/start")

            msg = ws.receive_json()
            assert msg == {
                "type": "confirm", "prompt": "Browse listings, or apply to whatever's open",
                "retryable": False, "allow_apply_current": True,
            }

            ws.send_json({"type": "confirm_response", "action": "apply_current"})

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "got action: apply_current"}


def test_reset_flags_soft_reset_when_nothing_pending(monkeypatch):
    # When the worker thread isn't blocked on a confirm/ask at all (e.g.
    # mid-autofill), soft reset can't answer anything directly -- it must
    # instead be observable via should_reset() at whatever the automation's
    # next safe checkpoint is.
    monkeypatch.setattr(webapp, "load_profile", lambda: {})

    async def polling_automation(profile, log=print, confirm_fn=None, ask_fn=None, on_frame=None,
                                  num_listings=5, max_steps=6, should_reset=None):
        log("run starting")
        for _ in range(100):
            if should_reset():
                log("reset flag observed")
                return
            await asyncio.sleep(0.02)
        log("reset flag never observed")

    app = webapp.create_app(run_automation_fn=polling_automation)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # initial state
            client.post("/start")

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "run starting"}

            resp = client.post("/reset")
            assert resp.status_code == 200

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "reset flag observed"}


def test_reset_skips_pending_ask_instead_of_hanging_forever(monkeypatch):
    # Regression: clicking Reset while a per-field review ("ask") prompt is
    # showing must not leave the worker thread permanently blocked inside
    # ask_fn's response.get() -- the frontend hides the ask panel on Reset
    # regardless of what the backend does, so there'd be no way to ever
    # answer it afterward, and the whole run would silently freeze. It
    # should skip that one field (like the review panel's own Skip button)
    # and flag the reset for should_reset() to pick up right after.
    monkeypatch.setattr(webapp, "load_profile", lambda: {})

    async def ask_then_poll_automation(profile, log=print, confirm_fn=None, ask_fn=None, on_frame=None,
                                        num_listings=5, max_steps=6, should_reset=None):
        log("run starting")
        answer = ask_fn({"label": "Mystery Field", "reasoning": "test", "type": "text", "options": []})
        log(f"got answer: {answer!r}")
        for _ in range(100):
            if should_reset():
                log("reset flag observed")
                return
            await asyncio.sleep(0.02)
        log("reset flag never observed")

    app = webapp.create_app(run_automation_fn=ask_then_poll_automation)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # initial state
            client.post("/start")

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "run starting"}
            msg = ws.receive_json()
            assert msg["type"] == "ask"

            resp = client.post("/reset")
            assert resp.status_code == 200

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "got answer: ''"}
            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "reset flag observed"}


def test_retry_confirm_response_loops_back_before_continuing(monkeypatch):
    # Mirrors main.run_automation's actual retry loop shape: a retryable
    # confirm whose "retry" answer re-runs the fill step and asks again,
    # only moving on once the answer is anything else.
    monkeypatch.setattr(webapp, "load_profile", lambda: {})
    fill_count = {"n": 0}

    async def retry_loop_automation(profile, log=print, confirm_fn=None, ask_fn=None, on_frame=None,
                                     num_listings=5, max_steps=6, should_reset=None):
        while True:
            fill_count["n"] += 1
            log(f"filled attempt {fill_count['n']}")
            action = confirm_fn("Review the form, or retry", retryable=True)
            if (action or "").strip().lower() != "retry":
                break
        log("moving to next listing")

    app = webapp.create_app(run_automation_fn=retry_loop_automation)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # initial state
            client.post("/start")

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "filled attempt 1"}
            msg = ws.receive_json()
            assert msg == {
                "type": "confirm", "prompt": "Review the form, or retry", "retryable": True,
                "allow_apply_current": False,
            }

            ws.send_json({"type": "confirm_response", "action": "retry"})

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "filled attempt 2"}
            msg = ws.receive_json()
            assert msg == {
                "type": "confirm", "prompt": "Review the form, or retry", "retryable": True,
                "allow_apply_current": False,
            }

            ws.send_json({"type": "confirm_response", "action": "continue"})

            msg = ws.receive_json()
            assert msg == {"type": "log", "text": "moving to next listing"}
            msg = ws.receive_json()
            assert msg == {"type": "finished", "text": "Run finished."}

            assert fill_count["n"] == 2
