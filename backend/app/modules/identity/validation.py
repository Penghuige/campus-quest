# backend/app/modules/identity/validation.py
"""Pure input validation for identity flows (spec §5.2, §5.3).

Design decisions:

- Student numbers are matched with ``re.fullmatch(r"[0-9]+", value)``, never
  ``str.isdigit()``: ``isdigit()`` accepts full-width and other Unicode digit
  characters (``"１２３４５６".isdigit()`` is ``True``), which spec §5.2
  forbids. Only the explicit ASCII class is safe.
- ``validate_student_number`` does NOT strip surrounding whitespace. Spec
  §5.2 allows removing outer whitespace at login time; that is the caller's
  job, done once at the login boundary, so a padded number can never slip
  through format validation by accident. The validated string is returned
  unchanged and must be stored as a string: converting to ``int`` would drop
  leading zeros (spec §5.2).
- Nickname length is counted in Unicode grapheme clusters via the ``regex``
  package's ``\\X`` pattern (spec §5.3 "user-perceived characters"), not in
  code points or UTF-8 bytes: a ZWJ-joined family emoji is one user-perceived
  character spanning 7 code points. ``regex`` is the controller-approved
  dependency choice here because the standard library has no grapheme
  segmentation (Apache-2.0 AND CNRI-Python license, actively maintained).
- Nickname cleaning removes Unicode control/format characters (general
  categories Cc, Cf, Cs, Co, Cn) everywhere in the string but keeps ZWJ
  (U+200D): ZWJ is a format character yet load-bearing inside emoji sequences,
  and removing it would split one grapheme cluster into several. Legitimate
  whitespace inside the nickname (e.g. U+0020) is preserved; only the edges
  are trimmed.
"""

import re
import unicodedata

import regex  # type: ignore[import-untyped]

STUDENT_NUMBER_DEFAULT_MIN_LEN = 6
STUDENT_NUMBER_DEFAULT_MAX_LEN = 20

NICKNAME_MAX_GRAPHEME_CLUSTERS = 16

_STUDENT_NUMBER_PATTERN = re.compile(r"[0-9]+")

# Format characters that must survive cleaning despite being category Cf:
# ZWJ glues emoji sequences into a single user-perceived character (spec §5.3
# counts a ZWJ family emoji as one).
_GRAPHEME_JOINERS = frozenset({"‍"})


def _is_invisible(ch: str) -> bool:
    """Whether ``ch`` is a control/format/unassigned character to remove."""
    return unicodedata.category(ch).startswith("C") and ch not in _GRAPHEME_JOINERS


def validate_student_number(
    value: str,
    min_len: int = STUDENT_NUMBER_DEFAULT_MIN_LEN,
    max_len: int = STUDENT_NUMBER_DEFAULT_MAX_LEN,
) -> str:
    """Validate a student number (ASCII digits only) and return it unchanged.

    Rules (spec §5.2):

    - only ASCII digits ``0-9``; no spaces, hyphens, dots, full-width digits,
      or scientific-notation forms;
    - length must satisfy ``min_len <= len(value) <= max_len``
      (defaults 6-20; callers may configure);
    - leading zeros are preserved (the value is never coerced to ``int``);
    - outer whitespace is NOT stripped here: trimming at login is the
      caller's responsibility.

    Raises:
        ValueError: if the format or the configured length is violated.
    """
    if _STUDENT_NUMBER_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "student number must contain only ASCII digits 0-9 "
            f"(got {len(value)} characters)"
        )
    if not min_len <= len(value) <= max_len:
        raise ValueError(
            f"student number must be {min_len}-{max_len} digits (got {len(value)})"
        )
    return value


def normalize_nickname(value: str) -> str:
    """Clean and validate a nickname, returning the normalized string.

    Rules (spec §5.3):

    - remove invisible control/format characters everywhere except ZWJ
      (needed inside emoji sequences); legitimate inner whitespace is kept;
    - trim whitespace from the edges;
    - reject when empty after cleaning;
    - reject when longer than 16 grapheme clusters (user-perceived
      characters), counted with the ``regex`` package's ``\\X`` pattern.

    Raises:
        ValueError: if empty after cleaning or longer than 16 graphemes.
    """
    cleaned = "".join(ch for ch in value if not _is_invisible(ch)).strip()
    if not cleaned:
        raise ValueError("nickname must not be empty after trimming")
    grapheme_count = len(regex.findall(r"\X", cleaned))
    if grapheme_count > NICKNAME_MAX_GRAPHEME_CLUSTERS:
        raise ValueError(
            f"nickname must be at most {NICKNAME_MAX_GRAPHEME_CLUSTERS} "
            f"grapheme clusters (got {grapheme_count})"
        )
    return cleaned
