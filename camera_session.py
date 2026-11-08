"""Stream lifecycle, bounded camera nudges, and PNG framing for the desktop viewer."""

import math
import queue
import struct
import subprocess
import threading
import time

from provision_auth import LoginCancelled, ProvisionError, SignInRequired


def ticket_needs_renewal(ticket, now, margin=60):
    try:
        data = ticket.get("data", ticket)
        expiration = float(data["expirationTime"])
        return (not data.get("accessToken") or not data.get("id") or not math.isfinite(expiration) or
                expiration <= (now + margin) * 1000)
    except (AttributeError, KeyError, TypeError, ValueError):
        return True


def read_exact(stream, count):
    result = bytearray()
    while len(result) < count:
        chunk = stream.read(count - len(result))
        if not chunk:
            return None
        result.extend(chunk)
    return bytes(result)


def png_frames(stream):
    while True:
        signature = read_exact(stream, 8)
        if signature is None:
            return
        if signature != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Invalid video frame signature")
        image = bytearray(signature)
        while True:
            header = read_exact(stream, 8)
            if header is None:
                return
            length = struct.unpack(">I", header[:4])[0]
            if length > 16 * 1024 * 1024 or len(image) + length > 32 * 1024 * 1024:
                raise ValueError("Oversized video frame")
            body = read_exact(stream, length + 4)
            if body is None:
                return
            image.extend(header)
            image.extend(body)
            if header[4:] == b"IEND":
                yield bytes(image)
                break


class MotionWorker:
    """Execute one short movement at a time and always follow it with stop."""

    def __init__(self, move, notify, pulse=0.2):
        self.move, self.notify, self.pulse = move, notify, pulse
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.busy = False
        self.closed = False

    def request(self, direction):
        if direction not in {"left", "right", "up", "down", "stop"}:
            return False
        with self.lock:
            if self.closed:
                return False
            if direction == "stop":
                self.cancel.set()
            if self.busy:
                return direction == "stop"
            self.busy = True
            self.cancel.clear()
            self.thread = threading.Thread(target=self._execute, args=(direction,), daemon=True)
            self.thread.start()
        return True

    def _execute(self, direction):
        error = None
        try:
            self.notify("motion", "Moving " + direction if direction != "stop" else "Stopping…")
            if direction != "stop" and not self.cancel.is_set():
                self.move(direction)
                self.cancel.wait(self.pulse)
        except ProvisionError as caught:
            error = caught
        finally:
            try:
                self.move("stop")
            except ProvisionError as caught:
                error = error or caught
            with self.lock:
                self.busy = False
            if isinstance(error, SignInRequired):
                self.notify("needs_login", str(error))
            elif error:
                self.notify("motion_error", str(error))
            else:
                self.notify("motion", "Use the buttons or arrow keys to move the camera.")

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel.set()
            thread = self.thread
        if thread is not None:
            thread.join(timeout=8)


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class PlaybackSwitcher:
    """Stop an old camera before starting another, without blocking the UI."""

    def __init__(self):
        self.current = None
        self.active = None
        self.closed = False
        self.lock = threading.Lock()
        self.handoff = threading.Lock()

    def select(self, playback):
        with self.lock:
            if self.closed:
                playback.cancel()
                return
            previous, self.current = self.current, playback
            if previous is not None:
                previous.cancel()
            if self.active is not None and self.active is not previous:
                self.active.cancel()
        threading.Thread(target=self._replace, args=(playback,), daemon=True).start()

    def _replace(self, playback):
        with self.handoff:
            with self.lock:
                if self.closed or self.current is not playback:
                    return
            if self.active is not None:
                self.active.close()
                self.active = None
            with self.lock:
                if not self.closed and self.current is playback:
                    self.active = playback
                    playback.start()

    def cancel(self):
        with self.lock:
            if self.current is not None:
                self.current.cancel()
            if self.active is not None and self.active is not self.current:
                self.active.cancel()

    def close(self):
        with self.lock:
            self.closed = True
            if self.current is not None:
                self.current.cancel()
            if self.active is not None:
                self.active.cancel()
        with self.handoff:
            if self.active is not None:
                self.active.close()
                self.active = None


class StreamSession:
    def __init__(self, ticket_provider, receiver_command, ffmpeg, log_path, notify, frame_width=2304):
        self.ticket_provider, self.receiver_command = ticket_provider, receiver_command
        self.ffmpeg, self.log_path, self.notify = ffmpeg, log_path, notify
        self.frame_width = int(frame_width)
        if not 1 <= self.frame_width <= 8192:
            raise ValueError("Requested video width must be between 1 and 8192 pixels")
        self.frames = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.restart = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.last_frame = 0
        self.frame_count = 0

    def start(self):
        self.thread.start()

    def reconnect(self):
        self.restart.set()

    def close(self):
        self.stop.set()
        self.restart.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=20)

    def _publish_frames(self, output):
        try:
            for image in png_frames(output):
                self.last_frame = time.monotonic()
                self.frame_count += 1
                if self.frames.full():
                    try:
                        self.frames.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    self.frames.put_nowait(image)
                except queue.Full:
                    pass
        except (OSError, ValueError):
            pass

    def _stream(self, ticket):
        receiver = decoder = reader = None
        self.last_frame, self.frame_count = time.monotonic(), 0
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with self.log_path.open("ab", buffering=0) as logs:
            try:
                decoder = subprocess.Popen([
                    self.ffmpeg, "-hide_banner", "-loglevel", "error", "-probesize", "65536",
                    "-analyzeduration", "0", "-f", "hevc", "-i", "pipe:0",
                    # A startup preview must not fix every later PNG at preview resolution.
                    "-an", "-vf", f"scale={self.frame_width}:-2", "-c:v", "png", "-compression_level", "1",
                    "-f", "image2pipe", "pipe:1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=logs, creationflags=flags)
                receiver = subprocess.Popen(self.receiver_command + ["-duration", "0", "-out", "-"],
                                            stdout=decoder.stdin, stderr=logs, creationflags=flags)
                decoder.stdin.close()
                reader = threading.Thread(target=self._publish_frames, args=(decoder.stdout,), daemon=True)
                reader.start()
                live = False
                while not self.stop.wait(0.2):
                    if self.restart.is_set():
                        return "reconnect"
                    if ticket_needs_renewal(ticket, time.time()):
                        return "renew"
                    if self.frame_count and not live:
                        live = True
                        self.notify("live", "Live")
                    if receiver.poll() is not None or decoder.poll() is not None:
                        return "disconnected"
                    if time.monotonic() - self.last_frame > 25:
                        return "disconnected"
                return "closed"
            finally:
                self.notify("offline", None)
                stop_process(receiver)
                stop_process(decoder)
                if reader is not None:
                    reader.join(timeout=1)
                if decoder is not None and decoder.stdout is not None:
                    decoder.stdout.close()

    def _run(self):
        force, failures = False, 0
        while not self.stop.is_set():
            self.restart.clear()
            self.notify("status", "Renewing session…" if force else "Connecting…")
            try:
                ticket = self.ticket_provider(force)
                if self.stop.is_set():
                    break
                reason = self._stream(ticket)
                force = True
                if reason == "closed":
                    break
                if reason in {"renew", "reconnect"}:
                    failures = 0
                    continue
                failures = 0 if self.frame_count else failures
                raise ProvisionError("Connection interrupted")
            except LoginCancelled:
                self.notify("cancelled", "Sign-in cancelled. Select a camera to try again.")
                while not self.stop.wait(0.2) and not self.restart.is_set():
                    pass
                force = True
            except (ProvisionError, OSError, ValueError):
                failures += 1
                delay = min(30, 2 ** min(failures - 1, 5))
                self.notify("status", f"Connection unavailable. Retrying in {delay}s…")
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not self.stop.wait(0.1) and not self.restart.is_set():
                    pass
                force = True
