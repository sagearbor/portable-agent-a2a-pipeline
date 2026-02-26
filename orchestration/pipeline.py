"""
Pipeline orchestrator - Phase 1 (pure Python, no Azure compute needed)

Wires Agent 1 -> Agent 2 -> Agent 3 together.
This is the A2A handoff logic. In Phase 2 this same logic runs as a
thin Azure Container App that calls Azure-hosted agents instead of
local Python functions.

Usage:
    python -m orchestration.pipeline
    python -m orchestration.pipeline --folder "Jira-Requests"
"""

import argparse
import json
import time
from config.settings import PROVIDER
from agents import agent1_email, agent2_router, agent3_jira


def run_pipeline(folder: str = "Inbox") -> dict:
    """
    Run the full email -> route -> jira pipeline.
    Returns a summary dict of what happened.
    """
    start = time.time()

    print(f"\n{'#'*60}")
    print(f"  EMAIL -> JIRA PIPELINE")
    print(f"  Provider: {PROVIDER}")
    print(f"  Folder:   {folder}")
    print(f"{'#'*60}")

    # A2A hop 1: Email Reader
    email_extracts = agent1_email.run(folder=folder)

    # A2A hop 2: Router
    approved_items = agent2_router.run(email_extracts=email_extracts)

    # A2A hop 3: Jira Creator
    created_tickets = agent3_jira.run(approved_items=approved_items)

    elapsed = round(time.time() - start, 2)

    summary = {
        "provider":        PROVIDER,
        "folder":          folder,
        "emails_read":     len(email_extracts),
        "tickets_approved": len(approved_items),
        "tickets_created": len(created_tickets),
        "elapsed_seconds": elapsed,
        "tickets":         created_tickets,
    }

    print(f"\n{'#'*60}")
    print(f"  PIPELINE COMPLETE")
    print(f"  Emails read:      {summary['emails_read']}")
    print(f"  Tickets approved: {summary['tickets_approved']}")
    print(f"  Tickets created:  {summary['tickets_created']}")
    print(f"  Total time:       {elapsed}s  (provider: {PROVIDER})")
    print(f"{'#'*60}\n")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the email-to-jira agent pipeline")
    parser.add_argument("--folder", default="Inbox", help="Outlook folder to read from")
    args = parser.parse_args()

    result = run_pipeline(folder=args.folder)
    print(json.dumps(result, indent=2))
