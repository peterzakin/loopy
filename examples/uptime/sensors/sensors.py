import urllib.request

from loopy import sensor
from loopy.events import Incident  # generated from registry.yml — optional, for your typechecker

HEALTH_URL = "https://example.com/health"


@sensor(poll="5m", emits="Incident")
def health_check(req) -> Incident | None:
    """Poll the health endpoint; a healthy check emits nothing.

    Returning None keeps the bus quiet — only a non-200 (or an unreachable endpoint)
    becomes an `Incident`. Turning a raw signal into a typed event, and only when it
    matters, is the whole job of a sensor.
    """
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as resp:
            status = resp.status
    except Exception:
        status = 0  # unreachable, treat as down
    if status == 200:
        return None  # healthy, emit nothing
    return Incident(url=HEALTH_URL, status=status)
