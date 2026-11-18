"""Standalone traffic programs that scenario workloads install on lab nodes.

Each module is a self-contained Python 3 script (standard library only). NIKA
copies its source onto a node and runs it there, so a module must not import
anything from ``nika``.
"""
