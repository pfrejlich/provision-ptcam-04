"""Local sign-in window; credentials go directly to Provision over HTTPS."""

import queue
import threading

from provision_auth import ProvisionError, atomic_write


def login_window(client, serial, ticket_path, parent=None) -> bool:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    if parent is not None:
        root.transient(parent)
        root.grab_set()
    root.title("Provision Home — desktop sign-in")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = ttk.Frame(root, padding=24)
    frame.grid()
    ttk.Label(frame, text="Sign in to Provision Home", font=("Segoe UI", 12, "bold")).grid(
        row=0, column=0, columnspan=2, pady=(0, 16), sticky="w")
    email = tk.StringVar()
    password = tk.StringVar()
    remember = tk.BooleanVar(value=False)
    for row, (label, variable, mask) in enumerate([
        ("Provision Home email", email, ""), ("Password", password, "*")
    ], 1):
        ttk.Label(frame, text=label).grid(row=row * 2 - 1, column=0, columnspan=2, sticky="w")
        entry = ttk.Entry(frame, textvariable=variable, show=mask, width=48)
        entry.grid(row=row * 2, column=0, columnspan=2, pady=(4, 12), sticky="ew")
        if row == 1:
            entry.focus_set()
    ttk.Checkbutton(frame, text="Remember sign-in for automatic renewal", variable=remember).grid(
        row=5, column=0, columnspan=2, sticky="w")
    ttk.Label(frame, text="Saved sign-in is encrypted for your Windows account.").grid(
        row=6, column=0, columnspan=2, sticky="w", pady=(4, 12))
    status = tk.StringVar(value="Credentials are sent directly to Provision's EU service.")
    ttk.Label(frame, textvariable=status, wraplength=390).grid(row=7, column=0, columnspan=2, sticky="w")
    outcomes = queue.Queue()
    succeeded = False
    busy = False

    def work(user, secret, save):
        try:
            ticket = client.sign_in(user, secret, serial, remember=save)
            import json
            atomic_write(ticket_path, json.dumps(ticket).encode("utf-8"))
            outcomes.put((True, "Signed in. The desktop obtained a new camera ticket."))
        except ProvisionError as error:
            outcomes.put((False, str(error)))
        except Exception:
            outcomes.put((False, "Sign-in could not be completed. Please retry."))

    def submit(*_):
        nonlocal busy
        if busy:
            return
        if not email.get().strip() or not password.get():
            status.set("Enter your Provision Home email and password.")
            return
        busy = True
        button.state(["disabled"])
        status.set("Signing in and requesting a camera ticket…")
        threading.Thread(target=work, args=(email.get().strip(), password.get(), remember.get()), daemon=True).start()

    def poll():
        nonlocal succeeded, busy
        try:
            ok, message = outcomes.get_nowait()
        except queue.Empty:
            root.after(100, poll)
            return
        status.set(message)
        password.set("")
        busy = False
        if ok:
            succeeded = True
            root.after(1200, root.destroy)
        else:
            button.state(["!disabled"])
            root.after(100, poll)

    button = ttk.Button(frame, text="Sign in", command=submit)
    button.grid(row=8, column=0, sticky="ew", pady=(16, 0))
    ttk.Button(frame, text="Cancel", command=root.destroy).grid(row=8, column=1, sticky="e", pady=(16, 0))
    root.bind("<Return>", submit)
    root.after(100, poll)
    if parent is None:
        root.mainloop()
    else:
        parent.wait_window(root)
    return succeeded
