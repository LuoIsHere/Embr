"""Simplified Chinese interface text: English key -> translation (design doc §12.5).

Keys are the English source text; formatted strings use template keys with {0}, {1}…
(i18n.template_key). Words the glossary keeps in English stay English inside the
translations."""

TEXT: dict[str, str] = {}
