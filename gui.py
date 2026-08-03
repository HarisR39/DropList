"""
Desktop GUI for DropList (Run + Review tabs).

Playwright's automation needs its own asyncio event loop; Tkinter needs the
main thread for its own event loop. So the automation runs in a background
thread with its own asyncio loop, and talks to the GUI only through a
thread-safe queue.Queue -- Tkinter widgets are only ever touched from the
main thread, inside _poll_events() (scheduled via root.after()).

Two blocking call sites in main.run_automation() get bridged the same way:
  - confirm_fn(prompt): a plain "wait for acknowledgement" gate (the
    review-and-continue pause, the "did you apply?" popup).
  - ask_fn(item): needs an actual answer for a flagged field, rendered here
    as radio buttons / checkboxes / a text box depending on item["type"].
Both push a request (with a private response queue) onto the shared events
queue and then block on response_queue.get() -- safe because this all
happens on the worker thread, never on the GUI thread.
"""

import asyncio
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

from main import load_profile, run_automation


class DropListGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("DropList")
        root.geometry("900x650")

        self.events: queue.Queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._pending_confirm_response: queue.Queue | None = None
        self._pending_ask_response: queue.Queue | None = None
        self._ask_widgets: dict = {}

        self._build_widgets()
        self._poll_events()

    # ---------------------------------------------------------------- UI ---

    def _build_widgets(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        self.run_tab = ttk.Frame(self.notebook)
        self.review_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.run_tab, text="Run")
        self.notebook.add(self.review_tab, text="Review")

        controls = ttk.Frame(self.run_tab)
        controls.pack(fill="x", padx=8, pady=8)
        self.start_button = ttk.Button(controls, text="Start", command=self.start_run)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop_run, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))

        # Shown only while a confirm_fn() request is pending.
        self.confirm_frame = ttk.Frame(self.run_tab)
        self.confirm_label = ttk.Label(self.confirm_frame, text="", wraplength=820, justify="left")
        self.confirm_label.pack(side="left", padx=(0, 8))
        ttk.Button(self.confirm_frame, text="Continue", command=self._submit_confirm).pack(side="left")

        self.log_view = scrolledtext.ScrolledText(self.run_tab, state="disabled", wrap="word")
        self.log_view.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.review_placeholder = ttk.Label(
            self.review_tab, text="Nothing needs review right now.", padding=16
        )
        self.review_placeholder.pack(anchor="nw")
        self.review_form_frame = ttk.Frame(self.review_tab, padding=16)

    def _append_log(self, text: str) -> None:
        self.log_view.config(state="normal")
        self.log_view.insert("end", text + "\n")
        self.log_view.see("end")
        self.log_view.config(state="disabled")

    # ------------------------------------------------- worker-thread hooks ---
    # These three are called FROM the background thread. They must never touch
    # Tkinter widgets directly -- only ever put onto the thread-safe queue.

    def _log(self, message: str) -> None:
        self.events.put(("log", message))

    def _confirm_fn(self, prompt: str) -> None:
        response: queue.Queue = queue.Queue()
        self.events.put(("confirm", prompt, response))
        response.get()  # blocks the worker thread until the GUI thread answers

    def _ask_fn(self, item: dict) -> str:
        response: queue.Queue = queue.Queue()
        self.events.put(("ask", item, response))
        return response.get()  # blocks the worker thread until the GUI thread answers

    # ------------------------------------------------------- start / stop ---

    def start_run(self) -> None:
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self._append_log("Starting...")

        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                profile = load_profile()
                task = loop.create_task(
                    run_automation(profile, log=self._log, confirm_fn=self._confirm_fn, ask_fn=self._ask_fn)
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

    def stop_run(self) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        self.stop_button.config(state="disabled")

    # ------------------------------------------------------- event polling ---
    # Runs on the GUI thread only (scheduled via root.after), so this is the
    # only place that's allowed to touch Tkinter widgets.

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self._append_log(event[1])
        elif kind == "confirm":
            _, prompt, response = event
            self._pending_confirm_response = response
            self.confirm_label.config(text=prompt)
            self.confirm_frame.pack(fill="x", padx=8, pady=(0, 8), before=self.log_view)
        elif kind == "ask":
            _, item, response = event
            self._pending_ask_response = response
            self._show_ask_form(item)
            self.notebook.select(self.review_tab)
        elif kind == "finished":
            self._append_log(event[1])
            self.start_button.config(state="normal")
            self.stop_button.config(state="disabled")
        elif kind == "error":
            self._append_log(f"ERROR: {event[1]}")
            self.start_button.config(state="normal")
            self.stop_button.config(state="disabled")

    def _submit_confirm(self) -> None:
        if self._pending_confirm_response is not None:
            self._pending_confirm_response.put(None)
            self._pending_confirm_response = None
        self.confirm_frame.pack_forget()

    # ------------------------------------------------------ review rendering ---

    def _show_ask_form(self, item: dict) -> None:
        for child in self.review_form_frame.winfo_children():
            child.destroy()
        self.review_placeholder.pack_forget()
        self.review_form_frame.pack(fill="both", expand=True)

        ttk.Label(
            self.review_form_frame, text=item["label"], font=("", 12, "bold"), wraplength=820, justify="left"
        ).pack(anchor="w")
        if item.get("reasoning"):
            ttk.Label(
                self.review_form_frame, text=f"Reason: {item['reasoning']}",
                foreground="gray", wraplength=820, justify="left",
            ).pack(anchor="w", pady=(2, 8))

        field_type = item.get("type")
        options = item.get("options") or []
        self._ask_widgets = {}

        if field_type in ("select", "radio-group") and options:
            var = tk.StringVar(value="")
            for opt in options:
                ttk.Radiobutton(self.review_form_frame, text=opt, variable=var, value=opt).pack(anchor="w")
            self._ask_widgets["var"] = var
        elif field_type == "checkbox-group" and options:
            checks = []
            for opt in options:
                v = tk.BooleanVar(value=False)
                ttk.Checkbutton(self.review_form_frame, text=opt, variable=v).pack(anchor="w")
                checks.append((opt, v))
            self._ask_widgets["checks"] = checks
        elif field_type == "textarea":
            text_widget = tk.Text(self.review_form_frame, height=4, width=70)
            text_widget.pack(anchor="w", pady=4)
            self._ask_widgets["text_widget"] = text_widget
        else:
            entry = ttk.Entry(self.review_form_frame, width=70)
            entry.pack(anchor="w", pady=4)
            self._ask_widgets["entry"] = entry

        button_row = ttk.Frame(self.review_form_frame)
        button_row.pack(anchor="w", pady=(12, 0))
        ttk.Button(button_row, text="Submit", command=self._submit_ask).pack(side="left")
        ttk.Button(button_row, text="Skip", command=self._skip_ask).pack(side="left", padx=(6, 0))

    def _collect_ask_answer(self) -> str:
        widgets = self._ask_widgets
        if "var" in widgets:
            return widgets["var"].get()
        if "checks" in widgets:
            return ", ".join(opt for opt, v in widgets["checks"] if v.get())
        if "text_widget" in widgets:
            return widgets["text_widget"].get("1.0", "end").strip()
        if "entry" in widgets:
            return widgets["entry"].get().strip()
        return ""

    def _submit_ask(self) -> None:
        self._finish_ask(self._collect_ask_answer())

    def _skip_ask(self) -> None:
        self._finish_ask("")

    def _finish_ask(self, answer: str) -> None:
        if self._pending_ask_response is not None:
            self._pending_ask_response.put(answer)
            self._pending_ask_response = None
        for child in self.review_form_frame.winfo_children():
            child.destroy()
        self.review_form_frame.pack_forget()
        self.review_placeholder.pack(anchor="nw")


def main() -> None:
    root = tk.Tk()
    DropListGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
