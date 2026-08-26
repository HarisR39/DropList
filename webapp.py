"""
FastAPI web app for DropList -- replaces the Tkinter gui.py.

Same thread + queue.Queue bridge as gui.py: run_automation() runs in a
background thread with its own asyncio event loop (Playwright needs
ProactorEventLoop for subprocess support on Windows, kept independent of
whatever loop uvicorn ends up using -- and if a Playwright call ever wedges,
a separate thread means Start/Stop and the log view stay responsive instead
of the whole server freezing with it), and only ever talks to the web layer
through a thread-safe queue.Queue -- never touches FastAPI/Starlette state
directly.

A single long-lived broadcaster task (started once via the app lifespan, not
per-connection) drains that queue and fans events out to whichever WebSocket
client(s) are currently connected, while keeping a small in-memory snapshot
(recent log lines, last frame, currently-pending ask/confirm) so a page
refresh or reconnect mid-run doesn't lose anything.
"""

import asyncio
import base64
import queue
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from main import load_profile, run_automation

MAX_LOG_LINES = 500


def _get_or_none(q: queue.Queue, timeout: float):
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


class AutomationBridge:
    """Bridges main.run_automation()'s worker-thread-side hooks to whatever
    WebSocket client(s) are connected. See module docstring for the threading
    rationale."""

    def __init__(self, run_automation_fn=run_automation):
        self._run_automation_fn = run_automation_fn
        self.events: queue.Queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._shutdown = threading.Event()

        self._connections: set[WebSocket] = set()
        self._log_tail: list[str] = []
        self._last_frame: str | None = None  # base64-encoded JPEG
        self._pending: dict | None = None  # {"kind": "confirm"|"ask", ...}
        self._pending_response: queue.Queue | None = None
        self._soft_reset_requested = threading.Event()

    @property
    def is_running(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.is_alive()

    # ------------------------------------------------- worker-thread hooks ---
    # Called FROM the background thread. Must never touch FastAPI/Starlette
    # state directly -- only ever put onto the thread-safe queue.

    def _log(self, message: str) -> None:
        self.events.put(("log", message))

    def _confirm_fn(self, prompt: str, retryable: bool = False, allow_apply_current: bool = False) -> str:
        response: queue.Queue = queue.Queue()
        self.events.put(("confirm", prompt, response, retryable, allow_apply_current))
        return response.get()  # blocks the worker thread until the browser answers

    def _ask_fn(self, item: dict) -> str:
        response: queue.Queue = queue.Queue()
        self.events.put(("ask", item, response))
        return response.get()  # blocks the worker thread until the browser answers

    def _on_frame(self, frame_bytes: bytes) -> None:
        self.events.put(("frame", base64.b64encode(frame_bytes).decode("ascii")))

    def _consume_soft_reset(self) -> bool:
        """Passed to run_automation as should_reset -- checking here also
        clears the flag, so a single soft_reset() request is consumed
        exactly once instead of re-triggering on every future pause too.
        Called from the worker thread; threading.Event is safe for that."""
        if self._soft_reset_requested.is_set():
            self._soft_reset_requested.clear()
            return True
        return False

    # ------------------------------------------------------- start / stop ---

    def start(self) -> bool:
        if self.is_running:
            return False
        self._log_tail = []
        self._last_frame = None
        self._pending = None
        self._pending_response = None
        self._soft_reset_requested.clear()

        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                profile = load_profile()
                task = loop.create_task(
                    self._run_automation_fn(
                        profile, log=self._log, confirm_fn=self._confirm_fn,
                        ask_fn=self._ask_fn, on_frame=self._on_frame,
                        should_reset=self._consume_soft_reset,
                    )
                )
                self._task = task
                loop.run_until_complete(task)
                self.events.put(("finished", "Run finished."))
            except asyncio.CancelledError:
                self.events.put(("finished", "Stopped."))
            except Exception as e:
                self.events.put(("error", str(e)))
            finally:
                loop.close()

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()
        return True

    def stop(self) -> bool:
        if not self.is_running or self._loop is None or self._task is None:
            return False
        if self._pending_response is not None:
            # The worker thread may be blocked inside a synchronous
            # confirm_fn/ask_fn wait (response.get() with no timeout) --
            # that occupies the worker's event loop entirely, so the
            # cancellation scheduled below has no chance to run until this
            # unblocks it first. None is safe for both: confirm_fn ignores
            # its return value, and ask_fn's caller already treats None like
            # "no answer" (`ask_fn(item) or ""`).
            self._pending_response.put(None)
        self._loop.call_soon_threadsafe(self._task.cancel)
        return True

    def soft_reset(self) -> bool:
        """Ask the running automation to jump back to the internship
        listing page and keep going on the SAME browser/login session,
        instead of stop()+start()'s full teardown-and-relaunch (see
        main.run_automation's should_reset/'reset' docs). Returns False if
        nothing is running -- there's nothing to reset back to.

        Takes effect immediately when a "confirm" prompt is currently
        pending, by answering it with a "reset" sentinel run_automation
        recognizes. An "ask" prompt (a per-field review question -- its
        answer becomes literal form text, not a sentinel run_automation
        would understand) is instead skipped, the same as the review
        panel's own Skip button -- answering it with "reset" or leaving it
        unanswered would either corrupt that field's value or leave the
        worker thread blocked forever inside ask_fn's response.get(),
        waiting for an answer nothing would ever send (the frontend hides
        the ask panel on Reset regardless, so there'd be no way to answer
        it afterward either). Either way, the actual reset is flagged for
        should_reset() to pick up as soon as run_automation reaches its
        next confirm() pause -- immediately after the skipped field in the
        "ask" case, or whenever nothing at all was pending."""
        if not self.is_running:
            return False
        if self._pending is not None and self._pending["kind"] == "confirm":
            self.submit_response("confirm", "reset")
        else:
            if self._pending is not None and self._pending["kind"] == "ask":
                self.submit_response("ask", "")
            self._soft_reset_requested.set()
        return True

    # ------------------------------------------------------- broadcasting ---
    # Runs on the app's own event loop (started once via lifespan).

    async def broadcaster_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._shutdown.is_set():
            item = await loop.run_in_executor(None, _get_or_none, self.events, 1.0)
            if item is not None:
                await self._handle_event(item)

    async def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self._log_tail.append(event[1])
            self._log_tail[:] = self._log_tail[-MAX_LOG_LINES:]
            message: dict[str, Any] = {"type": "log", "text": event[1]}
        elif kind == "frame":
            self._last_frame = event[1]
            message = {"type": "frame", "data": event[1]}
        elif kind == "confirm":
            _, prompt, response, retryable, allow_apply_current = event
            self._pending = {
                "kind": "confirm", "prompt": prompt, "retryable": retryable,
                "allow_apply_current": allow_apply_current,
            }
            self._pending_response = response
            message = {
                "type": "confirm", "prompt": prompt, "retryable": retryable,
                "allow_apply_current": allow_apply_current,
            }
        elif kind == "ask":
            _, item, response = event
            self._pending = {"kind": "ask", "item": item}
            self._pending_response = response
            message = {"type": "ask", "item": item}
        elif kind == "finished":
            self._pending = None
            self._pending_response = None
            message = {"type": "finished", "text": event[1]}
        elif kind == "error":
            self._pending = None
            self._pending_response = None
            message = {"type": "error", "text": event[1]}
        else:
            return
        await self._broadcast(message)

    async def _broadcast(self, message: dict) -> None:
        dead = set()
        for ws in self._connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        self._connections -= dead

    def state_snapshot(self) -> dict:
        """Sent to a client the moment it connects, so a page refresh or
        reconnect mid-run doesn't lose the pending prompt or log history."""
        snapshot: dict[str, Any] = {"type": "state", "log_tail": list(self._log_tail), "running": self.is_running}
        if self._last_frame is not None:
            snapshot["frame"] = self._last_frame
        if self._pending is not None:
            snapshot["pending"] = self._pending
        return snapshot

    def submit_response(self, kind: str, answer: str | None = None) -> bool:
        if self._pending is None or self._pending_response is None or self._pending["kind"] != kind:
            return False
        response = self._pending_response
        self._pending = None
        self._pending_response = None
        response.put(answer)
        return True


def create_app(run_automation_fn=run_automation) -> FastAPI:
    """Factory (rather than a bare module-level app) so tests can inject a
    fake run_automation_fn instead of the real Playwright/jobright.ai flow."""
    bridge = AutomationBridge(run_automation_fn=run_automation_fn)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broadcaster_task = asyncio.create_task(bridge.broadcaster_loop())
        yield
        bridge._shutdown.set()
        broadcaster_task.cancel()

    app = FastAPI(lifespan=lifespan)
    app.state.bridge = bridge

    @app.get("/")
    async def index():
        return FileResponse("static/index.html")

    @app.post("/start")
    async def start_run():
        if not bridge.start():
            return JSONResponse({"error": "already running"}, status_code=409)
        return {"status": "started"}

    @app.post("/stop")
    async def stop_run():
        if not bridge.stop():
            return JSONResponse({"error": "not running"}, status_code=400)
        return {"status": "stopping"}

    @app.post("/reset")
    async def reset_run():
        if not bridge.soft_reset():
            return JSONResponse({"error": "not running"}, status_code=400)
        return {"status": "reset"}

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        bridge._connections.add(websocket)
        try:
            await websocket.send_json(bridge.state_snapshot())
            while True:
                data = await websocket.receive_json()
                msg_type = data.get("type")
                if msg_type == "confirm_response":
                    bridge.submit_response("confirm", data.get("action", "continue"))
                elif msg_type == "ask_response":
                    bridge.submit_response("ask", data.get("answer", ""))
        except WebSocketDisconnect:
            pass
        finally:
            bridge._connections.discard(websocket)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
