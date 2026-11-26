"""Portal automation interface. Real browser clients plug in here (see base.py)."""

from rendiciones.automation.base import (
    ColmenaClient,
    PortalClient,
    PortalState,
    StatusResult,
    SubmissionResult,
)

__all__ = ["ColmenaClient", "PortalClient", "PortalState", "StatusResult", "SubmissionResult"]
