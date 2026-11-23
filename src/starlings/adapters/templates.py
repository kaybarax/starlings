"""Render the optional GitHub workflow templates for a project.

``templates/github-caller.yml`` is the project-owned caller: triggers
restricted to the refs the manifest routes to hosted gates, and a
``uses:`` reference to the reusable check workflow pinned exactly as
the manifest's ``github.reusable_workflow`` value. ``templates/
reusable-check.yml`` is the parameterized check job. ``starlings
templates github`` previews or writes them under ``.github/workflows``
— and nothing ever writes ``.github`` unless that command is run.
"""

from pathlib import Path

from ..batches import invalid
from .host import gate_base, routed_gates

DATA = Path(__file__).resolve().parent.parent / "templates"
CALLER = "github-caller.yml"
REUSABLE = "reusable-check.yml"


def render(policy):
    config = policy.get("github")
    if config is None:
        raise invalid("templates github requires a [github] table in starlings.toml")
    for key in ("workflow", "reusable_workflow"):
        if not config.get(key):
            raise invalid(f"templates github requires github.{key} in starlings.toml")
    branches = sorted({gate_base(policy, gate) for gate in routed_gates(policy)}) or [policy["release"]]
    caller = (DATA / CALLER).read_text(encoding="utf-8").replace("__BRANCHES__", ", ".join(branches)).replace("__REUSABLE__", config["reusable_workflow"])
    reusable = (DATA / REUSABLE).read_text(encoding="utf-8")
    reusable_name = Path(config["reusable_workflow"].split("@", 1)[0]).name
    return {config["workflow"]: caller, f".github/workflows/{reusable_name}": reusable}
