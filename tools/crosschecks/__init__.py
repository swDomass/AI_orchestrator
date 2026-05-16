"""Crosscheck submodules for the scientific-investigation tool.

Modules in this package implement the deterministic verification surface
that distinguishes T2 (audit-anchored) from T3 (LLM-only) crosschecks:

- audit_trail:           append-only JSONL audit log + helpers.
- externality_classifier: deterministic T2/T3 classification via SHA256 lookup.

Heavier modules (citation_verifier, adversarial_search, balance_runner,
falsification_check, cherrypicking_detector, similarity_index_updater,
engineering_reviewer) land in later increments.
"""
