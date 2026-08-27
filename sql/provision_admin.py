"""Provision the first SISDEV administrator from server-side environment values."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auditor.security import ensure_bootstrap_admin


def main() -> None:
    created = ensure_bootstrap_admin()
    if not created:
        raise SystemExit(
            "admin_not_created: configure the bootstrap variables and ensure no user exists"
        )
    print("admin_created")


if __name__ == "__main__":
    main()
