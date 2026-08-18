"""Freight Claim Copilot.

One narrow flow, end to end: a claim folder in NEGOTIATION status goes in,
an evidence-grounded Negotiation Position Brief comes out. Facts carry
verbatim citations that are mechanically verified against their sources;
arithmetic, source-authority rulings and entitlement math are deterministic;
the LLM is used only where language understanding is genuinely required.
"""

__version__ = "0.1.0"

PROMPTS_VERSION = "2026-08-18.1"
