"""Independent video tiles for single-camera and security station views."""


def fit_size(source, available):
    width, height = source
    scale = min(max(1, available[0]) / width, max(1, available[1]) / height)
    return max(1, int(width * scale)), max(1, int(height * scale))


def grid_shape(count, available):
    """Choose a grid that gives 16:9 video the largest combined display area."""
    if count < 1:
        return 1, 1
    width, height = max(1, available[0]), max(1, available[1])
    best = None
    for columns in range(1, count + 1):
        rows = (count + columns - 1) // columns
        scale = min(width / columns / 16, height / rows / 9)
        score = count * scale * scale
        candidate = (score, -rows * columns, rows, columns)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return best[2], best[3]


class CameraTile:
    def __init__(self, viewer, profile=None, playback=None):
        from tkinter import ttk

        tk = viewer.tk
        self.viewer, self.profile, self.playback = viewer, profile, playback
        self.image = self.source_image = None
        self.rendered_size = None
        self.resize_job = None
        self.frames = 0
        self.connected = False
        self.loading_active = False
        self.closed = False
        self.frame = tk.Frame(viewer.video_panel, background="#151719", highlightthickness=2,
                              highlightbackground="#343942", highlightcolor="#343942")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)
        self.status = tk.StringVar(master=viewer.root, value="Waiting")
        if profile is not None:
            header = tk.Frame(self.frame, background="#252a32", padx=10, pady=6)
            header.grid(row=0, column=0, sticky="ew")
            name = tk.Label(header, text=profile.name, background="#252a32", foreground="white",
                            font=("Segoe UI", 11, "bold"))
            name.pack(side="left")
            state = tk.Label(header, textvariable=self.status, background="#252a32", foreground="#bdc5d0")
            state.pack(side="right", padx=(12, 0))
            for widget in (header, name, state):
                widget.bind("<Button-1>", self.focus)
        self.canvas = tk.Canvas(self.frame, background="#151719", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.video_image = self.canvas.create_image(0, 0, anchor="center")
        self.video_text = self.canvas.create_text(0, 0, text="Choose a camera above" if profile is None else "",
                                                  fill="white", font=("Segoe UI", 16), justify="center")
        self.canvas.bind("<Configure>", self.resize)
        self.canvas.bind("<Button-1>", self.focus)
        self.loading_panel = ttk.Frame(self.canvas, padding=16)
        self.loading_message = tk.StringVar(master=viewer.root)
        ttk.Label(self.loading_panel, textvariable=self.loading_message, wraplength=240).pack(pady=(0, 10))
        self.loading_bar = ttk.Progressbar(self.loading_panel, mode="indeterminate", length=180)
        self.loading_bar.pack()

    def focus(self, event=None):
        if self.playback is not None:
            self.viewer.focus_camera(self.playback)
        self.canvas.focus_set()

    def set_focused(self, focused):
        self.frame.configure(highlightbackground="#62a9ff" if focused else "#343942",
                             highlightcolor="#62a9ff" if focused else "#343942")

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

    def resize(self, event=None):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.coords(self.video_image, width / 2, height / 2)
        self.canvas.coords(self.video_text, width / 2, height / 2)
        if self.resize_job is not None:
            self.viewer.root.after_cancel(self.resize_job)
        self.resize_job = self.viewer.root.after(75, self.render_resized)

    def render_resized(self):
        self.resize_job = None
        self.render()

    def render(self):
        if self.closed or self.source_image is None:
            return
        available = (self.canvas.winfo_width(), self.canvas.winfo_height())
        dimensions = fit_size(self.source_image.size, available)
        resized = self.source_image.resize(dimensions, self.viewer.Image.Resampling.BILINEAR)
        self.image = self.viewer.ImageTk.PhotoImage(resized, master=self.viewer.root)
        self.canvas.itemconfigure(self.video_image, image=self.image)
        self.canvas.coords(self.video_image, available[0] / 2, available[1] / 2)
        self.rendered_size = dimensions

    def display(self, image):
        self.source_image = image
        self.render()
        self.frames += 1
        self.connected = True
        self.status.set("Live")
        self.canvas.itemconfigure(self.video_text, text="")
        self.set_loading(False)

    def destroy(self):
        if self.closed:
            return
        self.closed = True
        self.loading_bar.stop()
        if self.resize_job is not None:
            self.viewer.root.after_cancel(self.resize_job)
        self.image = self.source_image = None
        self.frame.destroy()
