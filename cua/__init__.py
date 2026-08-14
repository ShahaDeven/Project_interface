"""Computer-use automation system — the software under evaluation.

Discovery is a compiler: an LLM drives a real UI once and the run is distilled into
a capability artifact. Replay is a VM: the artifact executes with no model in the
decision loop. The artifact is the bytecode, and `contracts/artifact.schema.json`
is the instruction set.

The target app in `target_app/` is a prop and is not part of this package.
"""

__version__ = "0.1.0"
