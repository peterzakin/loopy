from loopy import sensor
from loopy.events import CodeTask  # generated from registry.yml — optional, for your typechecker


@sensor(poll="10m", emits="CodeTask")  # `emits` is the contract the compiler reads
def task_queue(req) -> CodeTask:
    """Poll a task queue for code-change requests.

    A stub for the example — a real sensor would read from Linear, a GitHub issue label, a
    spreadsheet, etc., and return a CodeTask per request. Returning None emits nothing; this
    is here so `CodeTask` has a declared producer (and you can still fire one by hand with
    `loopy trigger --event CodeTask`).
    """
    return None
