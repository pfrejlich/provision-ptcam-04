"""Camera selection, responsive video, loading feedback, and directional controls."""

import io
import queue
import shutil
import threading

from camera_session import MotionWorker, PlaybackSwitcher, StreamSession


def fit_size(source, available):
    """Largest whole-pixel image that fits without stretching or cropping."""
    width, height = source
    limit_width, limit_height = available
    scale = min(max(1, limit_width) / width, max(1, limit_height) / height)
    return max(1, int(width * scale)), max(1, int(height * scale))


class Playback:
    def __init__(self, viewer, profile):
        camera = viewer.camera
        self.profile = profile
        self.access = camera.CameraAccess(profile)
        self.initial = viewer.args.refresh
        self.notify = lambda kind, value: viewer.events.put((self, kind, value))
        command = [str(camera.RECEIVER), "-ticket", str(self.access.ticket_path), "-serial", profile.serial,
                   "-target-ip", profile.camera_ip, "-resolution", viewer.args.resolution, "-size", viewer.args.size]
        self.session = StreamSession(self.prepare_ticket, command, viewer.ffmpeg,
                                     camera.STATE_ROOT / "logs" / "desktop-viewer.log", self.notify,
                                     frame_width=viewer.args.resolution.split("x")[0])
        self.motion = MotionWorker(lambda direction: camera.cloud_client(timeout=3).rotate(profile.serial, direction),
                                   self.notify)

    def prepare_ticket(self, force):
        initial, self.initial = self.initial, False
        return self.access.ensure_ticket(force or initial, login=self.request_login)

    def request_login(self):
        request = {"event": threading.Event(), "success": False}
        self.notify("login", request)
        while not request["event"].wait(0.1):
            if self.session.stop.is_set():
                return False
        return request["success"]

    def start(self):
        self.session.start()

    def cancel(self):
        self.session.stop.set()
        self.session.restart.set()
        self.motion.cancel.set()

    def close(self):
        self.cancel()
        self.motion.close()
        self.session.close()


class Viewer:
    def __init__(self, args):
        import tkinter as tk
        from tkinter import ttk
        from PIL import Image, ImageTk
        import camera_client as camera

        self.args = args
        self.camera, self.tk = camera, tk
        self.Image, self.ImageTk = Image, ImageTk
        self.ffmpeg = shutil.which("ffmpeg")
        if not self.ffmpeg:
            raise RuntimeError("FFmpeg is required to display camera video")
        self.cameras = tuple(getattr(args, "cameras", None) or camera.load_cameras())
        self.switcher = PlaybackSwitcher()
        self.playback = None
        self.root = tk.Tk()
        self.root.title("Provision cameras")
        self.root.geometry("1100x760")
        self.root.minsize(720, 520)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.events = queue.Queue()
        self.closed = False
        self.login_open = False
        self.pending_login = None
        self.image = None
        self.source_image = None
        self.rendered_size = None
        self.resize_job = None
        self.displayed_frames = 0
        self.connected = False
        self.loading_active = False
        self.status = tk.StringVar(value="Select a camera to start watching.")
        self.motion_status = tk.StringVar(value="Use the buttons or arrow keys to move the selected camera.")
        toolbar = ttk.Frame(self.root, padding=(16, 12))
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="Camera").pack(side="left", padx=(0, 10))
        self.selection = tk.StringVar(value="")
        self.selector = ttk.Combobox(toolbar, textvariable=self.selection, state="readonly", width=36,
                                     values=[profile.name for profile in self.cameras])
        self.selector.pack(side="left", fill="x", expand=True)
        self.selector.bind("<<ComboboxSelected>>", self.select_camera)
        self.video = tk.Canvas(self.root, background="#151719", highlightthickness=0)
        self.video.grid(row=1, column=0, sticky="nsew")
        self.video_image = self.video.create_image(0, 0, anchor="center")
        self.video_text = self.video.create_text(0, 0, text="Choose a camera above", fill="white",
                                                  font=("Segoe UI", 16))
        self.video.bind("<Configure>", self.resize_video)
        self.loading_panel = ttk.Frame(self.video, padding=20)
        self.loading_message = tk.StringVar()
        ttk.Label(self.loading_panel, textvariable=self.loading_message).pack(pady=(0, 12))
        self.loading_bar = ttk.Progressbar(self.loading_panel, mode="indeterminate", length=240)
        self.loading_bar.pack()
        footer = ttk.Frame(self.root, padding=12)
        footer.grid(row=2, column=0, sticky="ew")
        ttk.Label(footer, textvariable=self.status).pack(anchor="w")
        controls = ttk.Frame(footer)
        controls.pack(pady=4)
        self.buttons = []
        self.direction_buttons = {}
        for direction, label, row, column in [
            ("up", "↑ Up", 0, 1), ("left", "← Left", 1, 0),
            ("stop", "Stop", 1, 1), ("right", "Right →", 1, 2), ("down", "↓ Down", 2, 1)
        ]:
            button = ttk.Button(controls, text=label, width=12,
                                command=lambda selected=direction: self.move(selected))
            button.grid(row=row, column=column, padx=3, pady=2)
            button.state(["disabled"])
            self.buttons.append(button)
            self.direction_buttons[direction] = button
        ttk.Label(footer, textvariable=self.motion_status).pack(anchor="center")
        for key, direction in [("Left", "left"), ("Right", "right"), ("Up", "up"), ("Down", "down"), ("space", "stop")]:
            self.video.bind("<KeyPress-" + key + ">", lambda event, selected=direction: self.key_move(selected))
            self.root.bind("<KeyPress-" + key + ">", lambda event, selected=direction: self.key_move(selected))
        self.video.bind("<Button-1>", lambda event: self.video.focus_set())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_job = self.root.after(30, self.poll)
        self.selector.focus_set()

    @property
    def session(self):
        return self.playback.session if self.playback is not None else None

    @property
    def motion(self):
        return self.playback.motion if self.playback is not None else None

    def select_camera(self, event=None):
        if self.closed or self.login_open:
            return
        selected = self.selector.current()
        if selected < 0:
            return
        profile = self.cameras[selected]
        self.set_connected(False)
        self.root.title(profile.name + " — Provision camera")
        self.source_image = self.image = None
        self.video.itemconfigure(self.video_image, image="")
        self.video.itemconfigure(self.video_text, text="")
        self.status.set("Connecting to " + profile.name + "…")
        self.motion_status.set("Use the buttons or arrow keys to move the selected camera.")
        self.set_loading(True, "Loading " + profile.name + "…")
        self.playback = Playback(self, profile)
        self.switcher.select(self.playback)
        self.video.focus_set()

    def key_move(self, direction):
        # Native combo-box keyboard navigation must not move a camera.
        if self.root.focus_get() is self.selector or self.login_open:
            return
        self.move(direction)
        return "break"

    def move(self, direction):
        if self.connected and not self.login_open and self.motion is not None:
            self.motion.request(direction)

    def set_connected(self, value):
        self.connected = value
        for button in self.buttons:
            button.state(["!disabled"] if value else ["disabled"])

    def set_loading(self, active, message="Loading camera…"):
        self.loading_message.set(message)
        if active:
            self.loading_panel.place(relx=0.5, rely=0.5, anchor="center")
            if not self.loading_active:
                self.loading_bar.start(12)
        else:
            self.loading_bar.stop()
            self.loading_panel.place_forget()
        self.loading_active = active

    def resize_video(self, event=None):
        width, height = self.video.winfo_width(), self.video.winfo_height()
        self.video.coords(self.video_image, width / 2, height / 2)
        self.video.coords(self.video_text, width / 2, height / 2)
        if self.resize_job is not None:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(75, self.render_resized)

    def render_resized(self):
        self.resize_job = None
        self.render()

    def render(self):
        if self.closed or self.source_image is None:
            return
        available = (self.video.winfo_width(), self.video.winfo_height())
        dimensions = fit_size(self.source_image.size, available)
        resized = self.source_image.resize(dimensions, self.Image.Resampling.BILINEAR)
        self.image = self.ImageTk.PhotoImage(resized, master=self.root)
        self.video.itemconfigure(self.video_image, image=self.image)
        self.video.coords(self.video_image, available[0] / 2, available[1] / 2)
        self.rendered_size = dimensions

    def show_login(self, playback, request):
        from desktop_login import login_window
        self.login_open = True
        self.pending_login = request
        self.selector.state(["disabled"])
        self.status.set("Sign in to continue")
        self.set_connected(False)
        try:
            request["success"] = login_window(self.camera.cloud_client(), playback.profile.serial,
                                              playback.access.ticket_path, parent=self.root)
        finally:
            request["event"].set()
            self.pending_login = None
            self.login_open = False
            if not self.closed:
                self.selector.state(["!disabled", "readonly"])

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                playback, kind, value = self.events.get_nowait()
                if playback is not self.playback:
                    if kind == "login":
                        value["event"].set()
                    continue
                if kind == "status":
                    self.status.set(value)
                    self.set_loading(True, value)
                elif kind == "offline":
                    self.set_connected(False)
                    self.set_loading(True, "Reconnecting " + playback.profile.name + "…")
                    if self.motion.busy:
                        self.motion.request("stop")
                elif kind in {"motion", "motion_error"}:
                    self.motion_status.set(value)
                elif kind == "needs_login":
                    self.set_connected(False)
                    self.set_loading(True, "Renewing session…")
                    self.session.reconnect()
                elif kind == "login":
                    self.show_login(playback, value)
                    if self.closed:
                        return
                elif kind == "cancelled":
                    self.switcher.cancel()
                    self.playback = None
                    self.selection.set("")
                    self.set_connected(False)
                    self.set_loading(False)
                    self.source_image = self.image = None
                    self.video.itemconfigure(self.video_image, image="")
                    self.video.itemconfigure(self.video_text, text="Choose a camera above")
                    self.status.set(value)
        except queue.Empty:
            pass
        if self.closed:
            return
        if self.session is not None:
            try:
                frame = self.session.frames.get_nowait()
                with self.Image.open(io.BytesIO(frame)) as image:
                    self.source_image = image.convert("RGB")
                self.render()
                self.displayed_frames += 1
                self.video.itemconfigure(self.video_text, text="")
                self.set_loading(False)
                if not self.login_open:
                    self.set_connected(True)
                    self.status.set("Live — " + self.playback.profile.name)
            except queue.Empty:
                pass
            except (OSError, ValueError, self.tk.TclError):
                if not self.closed:
                    self.set_loading(True, "Reconnecting video…")
                    self.session.reconnect()
        self.poll_job = self.root.after(30, self.poll)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.pending_login is not None:
            self.pending_login["event"].set()
        self.switcher.cancel()
        self.loading_bar.stop()
        for job in (self.poll_job, self.resize_job):
            if job is not None:
                self.root.after_cancel(job)
        self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.closed = True
            self.switcher.close()
        return 0


def watch(args):
    return Viewer(args).run()
