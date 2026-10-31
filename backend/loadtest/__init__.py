"""On-demand load-test suite.

Spec: docs/superpowers/specs/2026-10-29-load-testing-design.md.

Not a pytest lane: scenarios run via locust against the local compose
stack; `make loadtest-stack-up` is the canonical SUT bring-up.
"""
