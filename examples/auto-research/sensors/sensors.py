"""arXiv poll sensor — the front of the research loop.

Every few hours it asks arXiv for papers newer than the last poll and emits one
`PaperFound` per result. A sensor may `yield` an iterator of events, so one poll fans out
into many `PaperFound` events — each one independently drives the digest → ideate → … loop.

This is a stub: it shows the shape (poll cadence + multi-emit) but returns nothing, so the
example stays runnable offline and you can also fire a `PaperFound` by hand with
`loopy trigger`. A real implementation would hit the arXiv Atom API (export.arxiv.org/api/query),
filter by category and submission date > req.last_run, and yield a PaperFound per entry.
"""

from collections.abc import Iterator

from loopy import sensor
from loopy.events import PaperFound  # generated from registry.yml — optional, for your typechecker


@sensor(poll="6h", emits="PaperFound")
def arxiv_poll(req) -> Iterator[PaperFound]:
    """Emit a PaperFound per new paper since the last poll.

    Sketch of the real thing:

        import urllib.request, feedparser
        q = "http://export.arxiv.org/api/query?search_query=cat:cs.LG&sortBy=submittedDate&sortOrder=descending&max_results=20"
        feed = feedparser.parse(urllib.request.urlopen(q).read())
        for e in feed.entries:
            if e.published <= req.last_run:   # only what changed since we last looked
                break
            yield PaperFound(
                paper_id=e.id.rsplit("/", 1)[-1],
                title=e.title.strip(),
                url=e.link,
                abstract=e.summary.strip(),
            )
    """
    return
    yield  # pragma: no cover — marks this a generator; the stub emits nothing
