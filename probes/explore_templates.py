"""
Tests whether the approval-template marketplace endpoints (found in
browser traffic while browsing the Templates tab) also work via
api.jotform.com — following the same pattern as every other workflow
endpoint in this project. These look like public gallery content (not
account-specific), so they might not even be origin-restricted the way
the workflow CRUD endpoints are.

Run: python -m probes.explore_templates
"""
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")


def main() -> None:
    probe(
        "GET", f"{BASE}/approval-template/category",
        label="template_categories", surface="public-api",
        params={"language": "en"},
    )

    probe(
        "GET", f"{BASE}/approval-templates/filter",
        label="template_filter_listing", surface="public-api",
        params={
            "rpp": 24, "sorting": "popular", "filterListing": "all",
            "start": 0, "filterStatus": "public", "noESign": 0,
            "language": "en", "category": "homepage",
        },
    )

    probe(
        "GET", f"{BASE}/approval-templates/languages",
        label="template_languages", surface="public-api",
    )

    # New: single-template full detail, found in the 3rd HAR — contains
    # the complete workflow snapshot (elements + links), not just a
    # truncated preview like the filter listing above.
    probe(
        "GET", f"{BASE}/approval-templates",
        label="template_single_detail", surface="public-api",
        params={"id": "242951093989068"},
    )


if __name__ == "__main__":
    main()
