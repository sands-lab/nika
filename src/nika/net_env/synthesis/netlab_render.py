"""Render netlab's configuration templates from a changed model snapshot.

``run_netlab`` runs this module in the netlab working directory after
``netlab create`` wrote ``netlab.snapshot.pickle``. It applies the design
changes to the transformed model and writes ``node_files`` with netlab's own
configuration output, and the changed model to ``rendered.json``.
"""

import json
import sys
import time

from netsim.data import global_vars
from netsim.outputs import _TopologyOutput
from netsim.utils import read

from nika.net_env.synthesis.changes import DesignChange, apply_model_changes


def main(changes_file: str) -> None:
    changes = [DesignChange.model_validate(c) for c in json.load(open(changes_file))]
    model = read.load_pickled_data("netlab.snapshot.pickle")
    # As netlab's own commands do when they load a snapshot.
    model.defaults._cache.timestamp = time.time()
    global_vars.init(model)
    apply_model_changes(model, changes)
    for name in ("config", "json=rendered.json"):
        _TopologyOutput.load(
            name, model.defaults.outputs.get(name.split("=")[0], {})
        ).write(model)


if __name__ == "__main__":
    main(sys.argv[1])
