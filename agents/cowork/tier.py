"""Which sandbox account a job runs as."""
from __future__ import annotations


def tier_for(job: dict) -> str:
    """The sandbox account a job runs as."""
    project = job["project"]
    if project == "friends":
        return "guest"  # chats from before friends had their own accounts
    if project.startswith("friend-"):
        return project
    return "owner"

