"""Readers for the memory stores memdebug watches, and the two rollback engines.

The readers (markdown_git, folder, openwebui, mem0) only read; what they return came from an untrusted store. The only
modules that write to a store are the rollback engines in restore.py (git) and folder_restore.py (plain folder), which
share the file-writing code in fileops.py.
"""
