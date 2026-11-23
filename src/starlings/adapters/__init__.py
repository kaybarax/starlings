"""Optional host adapters for hosted (executor ``github``) verification.

``host.py`` defines the adapter contract and shared helpers (binding,
routing, PR-disposition recording, doctor probe). ``fake_github.py`` is
the offline-qualified deterministic host used by tests and local
rehearsal; ``github.py`` is the real adapter's stub, which raises
``hosted-unavailable`` until the approved live rehearsal. No adapter
opens sockets: the fake is Python calls over a JSON event store in the
state directory.
"""
