"""Multi-band selector using the shared scrolling choice menu."""

from ..filtering import BAND_LABELS
from .selection_menu import SelectionMenu


class BandMenu(SelectionMenu):
    def __init__(self, bands: frozenset[str] | None):
        super().__init__(bands, BAND_LABELS, "Bands", "All bands")
