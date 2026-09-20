# HTTP API reference

Everything the app can do is reachable over HTTP. The browser UI and the `arducam`
CLI are both clients of this API and nothing else: neither of them imports a service
class or touches hardware, so a third client (an iPhone app, a Home Assistant
integration, a cron job that takes a picture every morning) needs no Python at all.

* Base URL: `http://<pi>:<port>` (default port `8080`, see `server.port` in the config)
* Prefix: `/api/v1` (also exported as `arducam_ptz.api.app.API_PREFIX`; `/api/version`
  lives outside it so a client can discover the prefix before it knows anything else)
* Interactive docs while the server runs: `/docs` (Swagger UI) and `/openapi.json`
* All bodies are JSON; all errors are JSON (§ Errors)

## Authentication

Off by default. With `server.token` set in the config, every `/api/v1` request needs the
token, either as a header or - for requests a browser makes by itself, such as the
`<img src>` of the preview - as a query parameter:

```
X-API-Token: <token>
GET /api/v1/stream.mjpg?token=<token>
```

The token is compared in constant time. It is a shared secret, not a user system: it
exists so that a camera on your LAN is not open to the whole LAN. Put the server behind
a reverse proxy with TLS if it can be reached from outside.

## Errors

Failures use one shape, so a client needs one code path (see `api/errors.py`):

```json
{"error": {"code": "unavailable", "detail": "focus device is not present", "component": "focus"}}
```

| HTTP | `code`         | Meaning                                                        |
| ---- | -------------- | -------------------------------------------------------------- |
| 400  | `bad_request`  | Malformed JSON or a value out of range (pydantic detail inside) |
| 401  | `unauthorized` | Missing or wrong token                                          |
| 404  | `not_found`    | No such photo, job or route                                     |
| 409  | `conflict`     | The request cannot be honoured right now (e.g. a capture is in progress, or the servo writer is released and the axis is limp) |
| 422  | `validation`   | A body that parsed but failed a constraint (e.g. pan outside `clamps`) |
| 503  | `unavailable`  | Hardware is absent or failed; `component` says which           |

A hardware problem is *not* a 500: the server keeps running with that component marked
unavailable, which lets the UI grey out one panel instead of losing the whole page.

## State

### `GET /api/v1/state`

One poll for everything the UI shows: health, pan/tilt, focus and stream statistics.
Cheap (no hardware I/O beyond cached values) and therefore safe to poll a few times a
second. A UI that polled four endpoints would show four different moments in time.

```json
{
  "health": {"status": "ok", "version": "0.1.0", "uptime_s": 12.5, "config_source": "...",
             "components": [{"name": "camera", "available": true, "simulated": false, "detail": null}]},
  "ptz": {"available": true, "simulated": false, "detail": null,
          "pan":  {"angle": 12.5, "min": -80, "max": 80, "inverted": false, "released": false},
          "tilt": {"angle": 3.0,  "min": -35, "max": 45, "inverted": true,  "released": false},
          "clamps": [{"axis": "pan", "requested": 95.0, "applied": 80.0}]},
  "focus": {"available": true, "simulated": false, "position": 512, "min": 0, "max": 4095,
            "step": 1, "near_is_high": true, "device": "/dev/v4l-subdiv3", "detail": null,
            "autofocus": null},
  "stream": {"running": true, "width": 960, "height": 540, "fps": 26.9,
             "kilobytes_per_s": 310.4, "subscribers": 1, "sequence": 1841,
             "last_frame_age_s": 0.04}
}
```

* `ptz.clamps` is non-empty when a move was limited: the caller asked for 95° and got
  80°. Reported rather than silently dropped, so a client can show why the camera stopped.
* `focus.near_is_high` says which raw direction is "nearer" - never assume, see
  § Units and conventions.
* `focus.autofocus` is the last (or running) sweep job, or `null`.
* `stream.sequence` increases with every frame the camera produces; use it to detect a
  frozen preview (a browser that renders a still image happily shows a stale frame
  forever).

### `GET /api/v1/health`

The same health block on its own, for load balancers and monitoring.

## Pan and tilt

Angles are degrees, +pan is to the right *when looking at the scene* (i.e. away from the
camera's own left), +tilt is up. Both are clamped to the configured mechanical limits.

| Request                        | Body                                | Result |
| ------------------------------ | ----------------------------------- | ------ |
| `GET /api/v1/ptz`              | -                                   | current target state |
| `POST /api/v1/ptz/move`        | `{"pan": 10, "tilt": -5}`           | absolute move; either field may be omitted to leave that axis alone |
| `POST /api/v1/ptz/nudge`       | `{"pan": 2, "tilt": 0}`             | relative move |
| `POST /api/v1/ptz/center`      | -                                   | move to the configured centre |
| `POST /api/v1/ptz/release`     | -                                   | cut servo pulses: the axes go limp and can be moved by hand |

```bash
curl -s -X POST localhost:8080/api/v1/ptz/nudge -H 'content-type: application/json' \
     -d '{"pan": 3, "tilt": -2}'
curl -s localhost:8080/api/v1/ptz
```

Moves are *immediate*, not eased: the smoothness comes from the client sending many small
nudges (that is what the joystick does - ~20 per second). A "move" is a target state, not
a motion profile, which keeps the server's job trivial and the feel identical in every
client.

`release` is the panic button: the servos stop holding, the camera droops under its own
weight, and the state reports `released: true` until the next move. Useful for saving
power on a battery rig and for moving the rig by hand.

## Focus

| Request                             | Body                                  | Result |
| ----------------------------------- | ------------------------------------- | ------ |
| `GET /api/v1/focus`                 | -                                     | position, limits, running job |
| `POST /api/v1/focus`                | `{"position": 700}`                   | absolute focus |
| `POST /api/v1/focus/nudge`          | `{"steps": 24}` (or negative)         | relative focus, in raw units |
| `POST /api/v1/focus/autofocus`      | `{}` or `{"region": {"x":.., "y":.., "w":.., "h":..}}` | start a sweep, `202` + job |
| `GET /api/v1/focus/autofocus/{id}`  | -                                     | poll the job |
| `POST /api/v1/focus/autofocus/{id}/cancel` | -                              | stop the sweep, keep the best position found |

### Autofocus is a job, not a request

A sweep takes seconds, so `POST /api/v1/focus/autofocus` answers `202 Accepted`
immediately with a job:

```json
{"id": "af-3f2c1a", "kind": "autofocus", "status": "running", "progress": 0.0,
 "phase": "coarse", "created_at": 1774000000.1, "updated_at": 1774000000.1,
 "poll_url": "/api/v1/focus/autofocus/af-3f2c1a", "result": null, "error": null}
```

* `status`: `pending` → `running` → `completed` / `failed` / `cancelled`
* `progress`: 0-1, `phase`: human-readable ("coarse", "fine", "settling")
* `result.samples`: every (position, sharpness) sample, so a client can draw the curve
* `result.position`: chosen position (the sweep leaves the lens there)

The sweep works the way manual focus does on a real lens: step through the range, measure
image sharpness (variance of the Laplacian) on the preview frame for each step, then
refine around the best reading. That is why it needs the stream to be running.

`region` focuses on a part of the frame instead of the centre - normalised coordinates
with the origin top-left, which is what a tap on the video gives you. This is the "tap
where you want it sharp" feature.

A sweep is cancelled by any manual focus command (a human overriding it wins) and holds
the *best* position seen so far rather than the last one visited.

```bash
job=$(curl -s -X POST localhost:8080/api/v1/focus/autofocus -d '{}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
while true; do
  curl -s localhost:8080/api/v1/focus/autofocus/$job | python3 -c 'import json,sys; j=json.load(sys.stdin); print(j["status"], j["progress"])'
  # ...until status is terminal
done
```

## Preview

| Request                              | Result |
| ------------------------------------ | ------ |
| `GET /api/v1/stream.mjpg`            | endless `multipart/x-mixed-replace` MJPEG |
| `GET /api/v1/stream/snapshot.jpg`    | one frame, `image/jpeg` |
| `GET /api/v1/stream/stats`           | the `stream` block of `/state` |

The stream is what a browser uses, because `<img src="…/stream.mjpg">` renders a live
picture with no JavaScript at all. The framing is the one every browser expects:

```
--arducamframe\r\n
Content-Type: image/jpeg\r\n
Content-Length: 25963\r\n
\r\n
<25963 bytes of JPEG>
\r\n
```

Notes for a client author:

* The parts carry `Content-Length` because Chrome renders part *N* as soon as it is
  complete. Without it the browser buffers the whole response and shows nothing until the
  connection closes - the classic "my MJPEG page is blank" bug.
* Send `Cache-Control: no-store` and `X-Accel-Buffering: no` if you put a proxy in front;
  the server already does.
* A subscriber that stops reading does **not** slow the camera down: it misses frames
  (its queue is bounded and drops the oldest) and is dropped after enough of them.
* `stats.subscribers == 0` means nobody is watching; the camera *keeps running* (the
  preview size is what autofocus measures), which is a deliberate trade: a warm camera
  means no visible restart when the next viewer arrives.
* The stream is served at the configured preview size (default 960x540). It is not
  resized per client, so every viewer sees the same stream - see docs/CONFIGURATION.md.

## Photos

| Request                                      | Result |
| -------------------------------------------- | ------ |
| `POST /api/v1/captures`                      | take a 16 MP still, `201` + `Photo` |
| `GET /api/v1/captures?limit=60`              | list, newest first |
| `GET /api/v1/captures/{filename}`            | the full JPEG |
| `GET /api/v1/captures/{filename}?max_dim=320`| a downscaled JPEG (cached on disk) |
| `DELETE /api/v1/captures/{filename}`         | delete the photo and its thumbnails |

```json
{"filename": "20260920-101533-123456.jpg",
 "url": "/api/v1/captures/20260920-101533-123456.jpg",
 "thumb_url": "/api/v1/captures/20260920-101533-123456.jpg?max_dim=320",
 "bytes": 4211332, "created_at": "2026-09-20T10:15:33+00:00",
 "width": 4656, "height": 3496}
```

Filenames are validated against a strict pattern (and the resolved path is re-checked
against the capture directory) so that `?max_dim` and the filename can never become a path
traversal. A capture switches the sensor to full resolution, which interrupts the preview
for about half a second; the stream recovers on its own.

## Conventions, units and contracts

The things that are easy to get wrong, written down because every client needs them:

| Quantity          | Unit / rule |
| ----------------- | ----------- |
| pan, tilt         | degrees; clamped to `ptz.<axis>.min`/`.max`; `inverted` is already applied by the server |
| focus position    | raw V4L2 units, `0..4095`; `near_is_high` says which end is close, so "focus near" is `+1` **only if** `near_is_high` |
| focus steps       | raw units, may be negative; the driver may round to `focus.step` |
| region (tap-to-focus) | normalised `0..1`, origin top-left, `w`/`h` optional |
| times             | Unix seconds (floats) for jobs, ISO-8601 UTC strings for photos |
| angles in `/state` | the *target*, i.e. what was last asked for - not a measured position (the rig has no encoders) |

## CLI equivalents

The CLI is a thin wrapper over these calls, so anything below is a one-liner from a
shell. Its global options (``--url``, ``--token``, ``--json``) come *before* the
subcommand - ``arducam --json status``, not ``arducam status --json`` - because typer
parses the group callback first:

| API | CLI |
| --- | --- |
| `GET /state` | `arducam status` |
| `POST /ptz/move` | `arducam move --pan 20 --tilt -10` |
| `POST /ptz/nudge` | `arducam nudge --pan 5` |
| `POST /ptz/center` | `arducam center` |
| `POST /ptz/release` | `arducam release` |
| `POST /focus` | `arducam focus 700` |
| `POST /focus/nudge` | `arducam focus-nudge 24` |
| `POST /focus/autofocus` | `arducam autofocus --wait` |
| `GET /stream.mjpg` | `arducam stream -o out.mjpg` |
| `POST /captures` | `arducam capture --download shot.jpg` |
| `GET /captures` | `arducam photos` |
| `DELETE /captures/{f}` | `arducam delete-photo <file>` |
