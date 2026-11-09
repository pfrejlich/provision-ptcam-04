# Provision PTCam-04 desktop client

A Windows desktop client for Provision Home cameras with H.265 video,
pan/tilt controls, simultaneous security station viewing, and automatic session renewal.

Based on [bbielsa/vicostream](https://github.com/bbielsa/vicostream).
The original upstream history and [documentation](docs/upstream-README.md)
are retained.

## Setup

Requires Windows, Python 3.10+ with Tkinter, Go 1.21+, and FFmpeg on `PATH`.

```powershell
git clone https://github.com/pfrejlich/provision-ptcam-04.git
Set-Location provision-ptcam-04
python -m pip install -r requirements.txt
go build -o provisionprobe.exe ./cmd/provisionprobe
Copy-Item camera.example.json camera.local.json
```

Edit the ignored `camera.local.json` file with your own camera names, serials,
and LAN addresses. Its example addresses are reserved for documentation and
must be replaced. Remove any unused camera entries. All cameras must belong
to the Provision account used to sign in.

```json
{
  "account": {"country_no": "US", "time_zone": "UTC"},
  "cameras": [
    {"name": "Camera A", "serial": "YOUR_CAMERA_1_SERIAL", "camera_ip": "192.0.2.10"},
    {"name": "Camera B", "serial": "YOUR_CAMERA_2_SERIAL", "camera_ip": "192.0.2.11"}
  ]
}
```

Set `account.country_no` and `account.time_zone` to the values appropriate for
your Provision account. These values stay in the ignored local file. The
neutral defaults are `US` and `UTC`. Environment variables `PROVISION_COUNTRY`
and `PROVISION_TIME_ZONE` override the account settings.

The original single-camera configuration with top-level `serial` and
`camera_ip` fields is also supported. `PROVISION_SERIAL` and
`PROVISION_CAMERA_IP` can configure one camera; they cannot be combined with a
multi-camera list. Restart the viewer after editing configuration.
The default service endpoint is Provision's EU API.

## Watch and control

```powershell
python .\camera_client.py
```

Choose a camera from the selector to start video. No connection or ticket request
starts before selection. An animated loading indicator remains visible until
the first frame arrives. Selecting another camera stops the old stream and any
pending movement first. Streaming-ticket caches are separate for each camera;
the account sign-in is shared.

Select **All cameras — Security station** to watch all configured cameras at
once. Each tile has its own loading indicator, connection state, and automatic
reconnect and ticket renewal. The grid adapts when the window is resized.
Click a tile to select its movement controls; the highlighted border identifies
the controlled camera. Movements are never broadcast to every camera. Choose a
camera by name to return to single-camera view; the grid streams are stopped first.

Concurrent cameras share one sign-in prompt and account renewal. Cancelling
sign-in pauses affected tiles without repeatedly reopening the dialog; other
healthy tiles continue displaying. Select the view again to retry. Receiver and
decoder processes are separate for each camera, so simultaneous viewing uses
more CPU and bandwidth than single-camera viewing. The station overview decodes
each tile at up to 1280 pixels wide to reduce CPU load; selecting one camera
restores its full requested display resolution.

The direction buttons and arrow keys move the selected camera briefly, followed
by a stop command. Space stops movement. Resizing or maximizing fits the video
to the window while preserving its aspect ratio, without reconnecting. The
default requests 2304×1296 video; the camera may choose a lower source quality.
The decoder retains the requested width so that a startup preview does not
limit later frames. Enlarging beyond the source resolution scales locally.

`Watch-Camera.ps1` and `--watch` open the same viewer. A lower-quality request is:

```powershell
python .\camera_client.py --watch --resolution 1280x720 --size medium
```

Tickets renew automatically before expiration. Interrupted streams reconnect
with backoff. When credentials are needed, a local sign-in window opens and
playback resumes after sign-in. Cancelling returns to camera selection without
repeated prompts. Select a camera again to retry.

Remembered account credentials are optional and encrypted with Windows DPAPI.
The client obtains tickets directly from Provision; no phone, ADB connection,
or mobile application cache is required. Provision's cloud authentication and
signaling services remain necessary.

## Samples and diagnostics

```powershell
python .\camera_client.py --camera "Camera A" --sample .\capture.hevc --seconds 20
python .\camera_client.py --camera "Camera A" --refresh --check
```

With multiple configured cameras, use `--camera` for `--sample`, `--check`, or
`--login`. A single configured camera is selected automatically for these CLI
operations. The graphical viewer always waits for a selection.
Sample capture accepts 1–60 seconds and refuses to overwrite an existing file.
The optional `probe_signaling.py` diagnostic additionally requires `websockets`.

## Private data

Camera identities and account metadata belong in `camera.local.json` or the
environment. Tokens and optional remembered passwords use DPAPI. Session files
and logs are stored beneath `%USERPROFILE%\.cache\camera-inspection`; override
this with `PROVISION_STATE_DIR`. Streaming tickets are local credential files.

Local configuration, environment files, tickets, credential stores, recordings,
logs, machine-specific launchers, and local verification reports are excluded
from Git. Avoid adding local values to examples, tests, documentation, or commit
metadata. Public fixtures use synthetic identifiers and documentation addresses.

## Build and test

```powershell
go test ./...
go build -o provisionprobe.exe ./cmd/provisionprobe
python -m py_compile camera_client.py camera_config.py provision_auth.py desktop_login.py desktop_viewer.py camera_tile.py camera_session.py probe_signaling.py
```

Start the local HTTP simulation in one terminal:

```powershell
postman simulation run postman/simulations/provision-auth.sim.yaml --port 43862 --output ndjson --no-history
```

Then run the checks in another:

```powershell
$env:PROVISION_TEST_URL = 'http://127.0.0.1:43862/provision'
$env:PROVISION_GUI_TESTS = '1'
python -m unittest discover -s tests -v
```

The desktop checks use synthetic cameras and frames. HTTP checks exercise login,
account metadata, ticket renewal, reauthentication, redirects, error responses,
and independent ticket caches. Other checks cover configuration, movement stops,
selection, loading animation, resizing, stream switching, concurrent tiles,
focused movement controls, and coordinated sign-in across stream workers.

The mock establishes behavior against its modeled responses, not the current
provider's behavior. Its credentials are synthetic. No Postman cloud resources
are needed. The installed CLI may lack `postman status`; the local simulation
can still be run directly.

The camera-address check matches an address advertised by the authenticated peer;
it is a camera selection check rather than a separate cryptographic identity.
