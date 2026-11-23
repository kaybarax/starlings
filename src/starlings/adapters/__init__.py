"""Optional host adapters for hosted (executor ``github``) verification.

``host.py`` defines the adapter contract and shared helpers (binding,
routing, PR-disposition recording, doctor probe). ``fake_github.py`` is
the offline-qualified deterministic host used by tests and local
rehearsal; ``github.py`` is the real adapter, which submits workflow
dispatches through the ``gh`` CLI (no raw tokens, no sockets in this
process). The fake is Python calls over a JSON event store in the state
directory; the real adapter keeps a submission journal there too.
"""
