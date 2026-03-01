"""Shared utility functions."""

import re


def derive_slug(company_name: str) -> str:
    """Derive an AmbitionBox-style URL slug from a company name.

    Strips common suffixes (Pvt. Ltd., Inc., etc.) and normalizes to lowercase
    hyphenated format.
    """
    name = company_name.strip()
    for suffix in ("Pvt. Ltd.", "Pvt Ltd", "Private Limited", "Ltd.", "Ltd",
                   "Limited", "Inc.", "Inc", "Corp.", "Corp", "Corporation",
                   "LLP", "LLC", "Technologies", "Technology", "Solutions",
                   "Services", "India"):
        if name.lower().endswith(suffix.lower()):
            name = name[:len(name) - len(suffix)].strip()
            break  # Only strip one suffix (fixes research.py bug)
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    slug = re.sub(r'-+', '-', slug)
    return slug
