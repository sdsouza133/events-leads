"""The pages' icons, drawn inline so they need no icon font or extra request, and work under the strict CSP.

Each is a 24x24 stroke drawing (paths from Lucide, lucide.dev, ISC license). Icons are decorative: the text beside
them says what they mean, so they're hidden from screen readers.
"""

from markupsafe import Markup

PATHS = {
    "arrow-right": Markup('<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>'),
    "calendar": Markup(
        '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4"/><path d="M8 3v4"/><path d="M3 11h18"/>'
    ),
    "check": Markup('<path d="M20 6 9 17l-5-5"/>'),
    "download": Markup('<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>'),
    "external": Markup(
        '<path d="M15 3h6v6"/><path d="M10 14 21 3"/>'
        '<path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>'
    ),
    "globe": Markup(
        '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18"/>'
    ),
    "key": Markup(
        '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.8-9.8"/><path d="m16 7 3 3"/><path d="m19 4 2 2"/>'
    ),
    "linkedin": Markup(
        '<path d="M16 8a6 6 0 0 1 6 6v7h-4v-7a2 2 0 0 0-4 0v7h-4v-7a6 6 0 0 1 6-6z"/>'
        '<rect x="2" y="9" width="4" height="12"/><circle cx="4" cy="4" r="2"/>'
    ),
    "mail": Markup('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>'),
    "search": Markup('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
    "users": Markup(
        '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
        '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'
    ),
}
SVG = Markup(
    '<svg class="icon" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
    "{paths}</svg>"
)


def icon(name: str, size: int = 20, stroke: float = 2) -> Markup:
    """The icon as inline SVG, colored by the text around it (currentColor)."""
    return SVG.format(size=size, stroke=stroke, paths=PATHS[name])
