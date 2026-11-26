"""Notices: an authority's published order or statement, read by Ember.

    from beacon.notice import read_notice

    notice = read_notice(ember, pdf_bytes)
    notice.notice_type, notice.areas, notice.instruction

See `read` for the Ember call, what is kept of a notice, and why it is kept in the
authority's own words.
"""

from beacon.notice.read import (
    HAZARDS,
    MAX_PDF_BYTES,
    NOTICE_TYPES,
    ORDERS,
    TEXT_TYPES,
    Notice,
    NoticeError,
    is_pdf,
    read_form,
    read_notice,
)

__all__ = [
    "HAZARDS",
    "MAX_PDF_BYTES",
    "NOTICE_TYPES",
    "ORDERS",
    "TEXT_TYPES",
    "Notice",
    "NoticeError",
    "is_pdf",
    "read_form",
    "read_notice",
]
