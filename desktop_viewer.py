"""Single-camera and simultaneous security station viewing with shared sign-in."""

import io
import queue
import shutil

from camera_session import LoginCoordinator, MotionWorker, PlaybackGroup, PlaybackSwitcher, StreamSession
from camera_tile import CameraTile, fit_size, grid_shape


STATION_LABEL = "All cameras — Security station"


class Playback:
    def __init__(self, viewer, profile):
        self.viewer, self.profile = viewer, profile
        camera = viewer.camera
        self.access = camera.CameraAccess(profile)
        self.initial = viewer.args.refresh
        self.notify = lambda kind, value: viewer.events.put((self, kind, value))
        command = [str(camera.RECEIVER), "-ticket", str(self.access.ticket_path), "-serial", profile.serial,
                   "-target-ip", profile.camera_ip, "-resolution", viewer.args.resolution, "-size", viewer.args.size]
        frame_width = int(viewer.args.resolution.split("x")[0])
        if viewer.station_mode and len(viewer.cameras) > 1:
            frame_width = min(frame_width, 1280)
        self.session = StreamSession(self.prepare_ticket, command, viewer.ffmpeg,
                                     self.access.ticket_path.with_suffix(".log"), self.notify,
                                     frame_width=frame_width)
        self.motion = MotionWorker(lambda direction: camera.cloud_client(timeout=3).rotate(profile.serial, direction),
                                   self.notify)

    def prepare_ticket(self, force):
        initial, self.initial = self.initial, False
        observed = self.viewer.authentication.snapshot()
        return self.access.ensure_ticket(force or initial, login=lambda: self.viewer.authentication.request(
            self.notify, self.session.stop, observed))

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
        self.authentication = LoginCoordinator()
        self.playback = None
        self.tiles = {}
        self.station_mode = False
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
        self.layout_job = None
        self.grid_dimensions = (1, 1)
        self.displayed_frames = 0
        self.connected = False
        self.status = tk.StringVar(master=self.root, value="Select a camera or security station to start watching.")
        self.motion_status = tk.StringVar(master=self.root, value="Select a camera for movement controls.")
        toolbar = ttk.Frame(self.root, padding=(16, 12))
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="View").pack(side="left", padx=(0, 10))
        self.selection = tk.StringVar(master=self.root, value="")
        self.selector = ttk.Combobox(toolbar, textvariable=self.selection, state="readonly", width=40,
                                     values=[profile.name for profile in self.cameras] + [STATION_LABEL])
        self.selector.pack(side="left", fill="x", expand=True)
        self.selector.bind("<<ComboboxSelected>>", self.select_camera)
        self.video_panel = tk.Frame(self.root, background="#151719")
        self.video_panel.grid(row=1, column=0, sticky="nsew")
        self.video_panel.bind("<Configure>", self.queue_layout)
        self.idle_tile = CameraTile(self)
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
            self.root.bind("<KeyPress-" + key + ">", lambda event, selected=direction: self.key_move(selected))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_job = self.root.after(30, self.poll)
        self.layout_tiles()
        self.selector.focus_set()

    @property
    def active_tile(self):
        return self.tiles.get(self.playback) or next(iter(self.tiles.values()), self.idle_tile)

    @property
    def video(self):
        return self.active_tile.canvas

    @property
    def source_image(self):
        return self.active_tile.source_image

    @property
    def rendered_size(self):
        return self.active_tile.rendered_size

    @property
    def loading_active(self):
        return self.active_tile.loading_active

    @property
    def loading_bar(self):
        return self.active_tile.loading_bar

    @property
    def session(self):
        return self.playback.session if self.playback is not None else None

    @property
    def motion(self):
        return self.playback.motion if self.playback is not None else None

    def queue_layout(self, event=None):
        if self.layout_job is not None:
            self.root.after_cancel(self.layout_job)
        self.layout_job = self.root.after(75, self.layout_tiles)

    def layout_tiles(self):
        if self.layout_job is not None:
            self.root.after_cancel(self.layout_job)
        self.layout_job = None
        if self.closed:
            return
        tiles = list(self.tiles.values()) or [self.idle_tile]
        rows, columns = grid_shape(len(tiles), (self.video_panel.winfo_width(), self.video_panel.winfo_height()))
        old_rows, old_columns = self.grid_dimensions
        for row in range(max(rows, old_rows)):
            self.video_panel.rowconfigure(row, weight=1 if row < rows else 0,
                                          uniform="videos" if row < rows else "")
        for column in range(max(columns, old_columns)):
            self.video_panel.columnconfigure(column, weight=1 if column < columns else 0,
                                             uniform="videos" if column < columns else "")
        for index, tile in enumerate(tiles):
            tile.frame.grid(row=index // columns, column=index % columns, sticky="nsew", padx=2, pady=2)
        self.grid_dimensions = rows, columns

    def select_camera(self, event=None):
        if self.closed or self.login_open:
            return
        selected = self.selector.current()
        if selected < 0:
            return
        self.switcher.cancel()
        self.authentication.reset()
        self.playback = None
        self.set_connected(False)
        for tile in self.tiles.values():
            tile.destroy()
        self.tiles.clear()
        self.idle_tile.frame.grid_forget()
        self.station_mode = selected == len(self.cameras)
        profiles = self.cameras if self.station_mode else (self.cameras[selected],)
        for profile in profiles:
            playback = Playback(self, profile)
            tile = CameraTile(self, profile, playback)
            self.tiles[playback] = tile
            tile.set_loading(True, "Loading " + profile.name + "…")
        self.layout_tiles()
        self.root.title("Security station — Provision cameras" if self.station_mode else profiles[0].name + " — Provision camera")
        self.motion_status.set("Click a camera tile to choose its movement controls." if self.station_mode else
                               "Use the buttons or arrow keys to move the selected camera.")
        self.switcher.select(PlaybackGroup(self.tiles))
        if not self.station_mode:
            self.focus_camera(next(iter(self.tiles)))
        self.update_status()

    def focus_camera(self, playback):
        if self.closed or self.login_open or playback not in self.tiles:
            return
        if self.playback is not playback and self.motion is not None and self.motion.busy:
            self.motion.request("stop")
        self.playback = playback
        for candidate, tile in self.tiles.items():
            tile.set_focused(candidate is playback)
        self.set_connected(self.tiles[playback].connected)
        self.motion_status.set("Controls: " + playback.profile.name + " — arrow keys or buttons; Space stops.")
        self.tiles[playback].canvas.focus_set()

    def key_move(self, direction):
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
            button.state(["!disabled"] if value and not self.login_open else ["disabled"])

    def render(self):
        self.active_tile.render()

    def update_status(self):
        if self.station_mode and self.tiles:
            live = sum(tile.connected for tile in self.tiles.values())
            self.status.set(f"Security station — {live}/{len(self.tiles)} cameras live. Click a tile for controls.")
        elif self.playback is not None:
            tile = self.tiles[self.playback]
            self.status.set(("Live — " if tile.connected else tile.status.get() + " — ") + self.playback.profile.name)

    def show_login(self, playback, request):
        from desktop_login import login_window
        self.login_open = True
        self.pending_login = request
        self.selector.state(["disabled"])
        self.set_connected(False)
        self.status.set("Sign in to continue")
        success = False
        try:
            success = login_window(self.camera.cloud_client(), playback.profile.serial,
                                   playback.access.ticket_path, parent=self.root)
        finally:
            self.authentication.finish(request, success)
            self.pending_login = None
            self.login_open = False
            if not self.closed:
                self.selector.state(["!disabled", "readonly"])
                if self.playback is not None:
                    self.set_connected(self.tiles[self.playback].connected)

    def cancel_sign_in(self, playback, message):
        tile = self.tiles[playback]
        tile.connected = False
        playback.cancel()
        tile.set_loading(False)
        tile.status.set("Sign-in required")
        tile.canvas.itemconfigure(tile.video_text, text="Select this view again to sign in")
        if not self.station_mode:
            self.switcher.cancel()
            self.playback = None
            self.selection.set("")
            tile.destroy()
            self.tiles.clear()
            self.layout_tiles()
            self.status.set(message)
        if self.playback is playback or self.playback is None:
            self.set_connected(False)

    def handle_event(self, playback, kind, value):
        tile = self.tiles.get(playback)
        if tile is None:
            if kind == "login":
                self.authentication.finish(value, False)
            return
        if kind == "status":
            tile.connected = False
            tile.status.set(value)
            tile.set_loading(True, value)
        elif kind == "offline":
            tile.connected = False
            tile.status.set("Reconnecting…")
            tile.set_loading(True, "Reconnecting " + playback.profile.name + "…")
            if playback.motion.busy:
                playback.motion.request("stop")
        elif kind in {"motion", "motion_error"}:
            if playback is self.playback:
                self.motion_status.set(value)
        elif kind == "needs_login":
            tile.connected = False
            tile.status.set("Sign-in required")
            tile.set_loading(True, "Renewing session…")
            playback.session.reconnect()
        elif kind == "login":
            self.show_login(playback, value)
        elif kind == "cancelled":
            self.cancel_sign_in(playback, value)
        if not self.closed and self.playback is playback and playback in self.tiles:
            self.set_connected(tile.connected)

    def poll(self):
        if self.closed:
            return
        # Modal sign-in runs a nested Tk loop; keep other tiles rendering in it.
        self.poll_job = self.root.after(30, self.poll)
        try:
            while True:
                self.handle_event(*self.events.get_nowait())
                if self.closed:
                    return
        except queue.Empty:
            pass
        for playback, tile in list(self.tiles.items()):
            if playback.session.stop.is_set():
                continue
            try:
                frame = playback.session.frames.get_nowait()
                with self.Image.open(io.BytesIO(frame)) as image:
                    tile.display(image.convert("RGB"))
                self.displayed_frames += 1
                if playback is self.playback and not self.login_open:
                    self.set_connected(True)
            except queue.Empty:
                pass
            except (OSError, ValueError, self.tk.TclError):
                if not self.closed:
                    tile.connected = False
                    tile.status.set("Reconnecting video…")
                    tile.set_loading(True, "Reconnecting video…")
                    playback.session.reconnect()
        if not self.login_open:
            self.update_status()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.authentication.reset()
        self.switcher.cancel()
        for job in (self.poll_job, self.layout_job):
            if job is not None:
                self.root.after_cancel(job)
        for tile in [self.idle_tile, *self.tiles.values()]:
            tile.destroy()
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
