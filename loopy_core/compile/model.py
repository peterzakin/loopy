"""The IR root (`Project`) and the derived lineage graph.

`Project` mirrors the manifest's four top-level keys and is the single object the
pipeline threads and M5 serializes. `Lineage`/`EventLineage` are *derived* (built
by X5 in M5) and therefore span-exempt.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from loopy_core.registry.model import Registry
from loopy_core.sensors.model import Sensor
from loopy_core.workflow.model import Workflow


class EventLineage(BaseModel):
    # Sorted by X5 for a byte-stable manifest.
    producers: list[str] = Field(default_factory=list)
    consumers: list[str] = Field(default_factory=list)


class Lineage(BaseModel):
    events: dict[str, EventLineage] = Field(default_factory=dict)


class Project(BaseModel):
    # Workflow carries a networkx.DiGraph in `dag`.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    registry: Registry = Field(default_factory=Registry)
    workflows: dict[str, Workflow] = Field(default_factory=dict)
    sensors: list[Sensor] = Field(default_factory=list)
    lineage: Lineage = Field(default_factory=Lineage)
