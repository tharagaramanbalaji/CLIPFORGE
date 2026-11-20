"""Highlight detection: windowing, LLM prompts/schemas, deterministic validation, dedupe, boundaries, scoring.

The LLM only ever references transcript *segment IDs* (ADR-0001); every time is resolved and checked here.
"""
