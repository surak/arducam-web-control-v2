"""Arducam pan-tilt camera control - hardware layer, services, HTTP API and web UI.

Package layout (each layer only imports from the ones above it, never sideways):

    config        <- every deployment detail lives here, nothing is hard-coded
    models        <- the wire contract (pydantic), importable without any hardware
    hardware/     <- thin backends for PCA9685 servos, the AK7375 focus motor and
                     the IMX519 camera, plus a fully simulated set for tests
    services/     <- stateful logic: frame fan-out, pan/tilt state, focus +
                     autofocus jobs, photo storage
    api/          <- FastAPI app: thin HTTP/JSON wrappers over the services
    web/          <- static front-end (no build step) that only talks HTTP
    cli.py        <- terminal client that also only talks HTTP

The point of the split: `cli.py`, the web UI and a future iPhone app all talk to
the same documented HTTP API, so the hardware code exists exactly once.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
