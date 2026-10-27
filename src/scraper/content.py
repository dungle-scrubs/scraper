"""Pure content helpers shared by the configured and public CLI paths."""

import re

BOT_DETECTION_PHRASES = (
    "performing security verification",
    "verifies you are not a bot",
    # Tightened from bare "cloudflare"/"captcha" (F8): those are common words
    # that appear in legitimate content. These phrases name the challenge page
    # itself, so a real doc merely discussing the provider no longer trips it.
    "checking your browser before accessing",
    "captcha to continue",
    "complete the captcha",
    "enable javascript to continue",
    "this page was lost in training",
    "access denied",
    "just a moment",
)

# Strong, specific bot-challenge signals. Any one of these is decisive on its
# own (F8): these strings only appear on an actual challenge/interstitial page,
# so they flag a block regardless of heading presence or page length - a real
# doc discussing a provider never emits a "cf-browser-verification" token or a
# rendered "attention required! | cloudflare" heading.
STRONG_BOT_SIGNALS = (
    "cloudflare ray id",
    "cf-browser-verification",
    "attention required! | cloudflare",
)

# Challenge-page heuristic thresholds (see looks_like_bot_block). Requiring all
# three (phrase + no heading + short) avoids false positives on legitimate docs
# that merely mention a provider name like "cloudflare".
BOT_CHALLENGE_MAX_CHARS = 1500
BOT_CHALLENGE_MAX_WORDS = 150

# Partial-JS-render heuristic thresholds. A markdown table row with empty cells
# between the pipes (e.g. "|  |  |") signals the DOM rendered but the data did
# not. Whitespace inside the cells is collapsed before matching so tabs / varied
# padding don't defeat detection. Requiring a minimum count of empty-cell rows
# AND that empty cells DOMINATE the table (BOT_EMPTY_CELL_MIN_RATIO of all pipe
# rows) - plus the absence of a heading / substantive prose - keeps a legit 6+
# row comparison table with a couple of blanks from being flagged (F3).
BOT_EMPTY_CELL_MIN_ROWS = 5
BOT_EMPTY_CELL_MIN_RATIO = 0.7
# Words of non-table, non-heading text above which a page is treated as real
# content rather than a blank rendered table (F3).
BOT_TABLE_PROSE_MIN_WORDS = 25
_EMPTY_CELL_RE = re.compile(r"\|\s*\|")

SCRAPE_ARTIFACT_LINE_PATTERNS = (
    re.compile(r"^\[Skip to content\]\([^)]*\)$", re.IGNORECASE),
    re.compile(r"^Dismiss alert$", re.IGNORECASE),
    re.compile(r"^\{\{\s*message\s*\}\}$", re.IGNORECASE),
)

SCRAPE_ARTIFACT_LINE_SUBSTRINGS = (
    "You signed in with another tab or window.",
    "You signed out in another tab or window.",
    "You switched accounts on another tab or window.",
)


def is_scrape_artifact_line(line: str) -> bool:
    """Return True when a markdown line matches known scraper noise patterns.

    @param line: Single line of markdown text.
    @returns: True if this line is a scraper artifact that should be removed.
    """
    stripped = line.strip()
    if not stripped:
        return False

    if any(pattern.match(stripped) for pattern in SCRAPE_ARTIFACT_LINE_PATTERNS):
        return True

    return any(fragment in stripped for fragment in SCRAPE_ARTIFACT_LINE_SUBSTRINGS)


def clean_markdown_content(markdown: str) -> str:
    """Remove known scrape artifacts and normalize markdown whitespace.

    Fenced code blocks are emitted verbatim: while inside a fence, artifact
    stripping, adjacent-duplicate collapsing, and blank-run collapsing are all
    suspended so legitimate code (repeated `print(1)` lines, stray `}`, identical
    config lines, intentional blank lines) survives intact. A fence toggles on any
    line whose stripped form starts with a code fence marker (``` or ~~~). Only
    whole-document leading/trailing whitespace is trimmed.

    @param markdown: Raw markdown from any scraping source.
    @returns: Cleaned markdown with artifacts removed and whitespace normalized.
    """
    cleaned_lines: list[str] = []
    in_code_fence = False

    for line in markdown.splitlines():
        normalized = line.rstrip()

        stripped = normalized.lstrip()
        is_fence = stripped.startswith("```") or stripped.startswith("~~~")

        # Inside a fence, emit lines verbatim - no artifact stripping, no dedup.
        if in_code_fence:
            cleaned_lines.append(normalized)
            if is_fence:
                in_code_fence = False
            continue

        if is_fence:
            in_code_fence = True
            cleaned_lines.append(normalized)
            continue

        if is_scrape_artifact_line(normalized):
            continue

        # Collapse duplicate adjacent lines from page chrome extraction.
        if normalized and cleaned_lines and normalized == cleaned_lines[-1]:
            continue

        # Collapse blank-line runs to a single blank line. Done here (outside
        # fences) rather than with a whole-string regex so intentional blank
        # lines inside code blocks - emitted verbatim above - are preserved.
        # Also drops leading blank lines; trailing ones are trimmed below.
        if not normalized and (not cleaned_lines or cleaned_lines[-1] == ""):
            continue

        cleaned_lines.append(normalized)

    cleaned = "\n".join(cleaned_lines).strip()
    return f"{cleaned}\n" if cleaned else ""


# ── Bot detection ────────────────────────────────────────────


def looks_like_bot_block(markdown: str) -> bool:
    """Return True when scraped markdown looks like a bot-protection or failed-render page.

    Detects two failure modes:
    1. Cloudflare/CAPTCHA challenge pages (short, contains bot-protection phrases).
    2. Partial JS rendering - page structure loaded but dynamic data is missing,
       e.g. table rows with blank cells where content should be.

    @param markdown: Markdown from crawl4ai.
    @returns: True if the content should be re-fetched via Jina Reader.
    """
    lower = markdown.lower()

    # Strong, specific signals are decisive on their own (F8): these strings only
    # appear on an actual challenge/interstitial page, so they flag a block
    # regardless of the heading / length gating that governs the weaker phrases.
    if any(signal in lower for signal in STRONG_BOT_SIGNALS):
        return True

    has_heading = any(line.lstrip().startswith("#") for line in markdown.splitlines())

    # Challenge pages are short, heading-less, and terse. Requiring all three
    # avoids false positives on legitimate short docs that merely mention a
    # provider name like "cloudflare".
    has_phrase = any(phrase in lower for phrase in BOT_DETECTION_PHRASES)
    word_count = len(lower.split())
    if (
        has_phrase
        and not has_heading
        and len(markdown) < BOT_CHALLENGE_MAX_CHARS
        and word_count < BOT_CHALLENGE_MAX_WORDS
    ):
        return True

    # Partial JS rendering: rows with many empty cells between pipes. Cells are
    # matched after collapsing internal whitespace so tabs / varied padding
    # don't slip past the old "|  |" / "| |" substring check. To avoid flagging a
    # legit comparison table that carries a few blanks (F3), require BOTH a
    # minimum count of empty-cell rows AND that empty cells DOMINATE the table
    # (>= BOT_EMPTY_CELL_MIN_RATIO of all pipe rows), AND that the page has no
    # heading or substantive prose anchoring it as real content.
    pipe_rows = [line for line in markdown.splitlines() if "|" in line and "---" not in line]
    if pipe_rows:
        empty_cell_rows = sum(1 for row in pipe_rows if _EMPTY_CELL_RE.search(row))
        dominates = empty_cell_rows >= BOT_EMPTY_CELL_MIN_RATIO * len(pipe_rows)
        prose_words = sum(
            len(line.split())
            for line in markdown.splitlines()
            if line.strip() and "|" not in line and not line.lstrip().startswith("#")
        )
        has_substantive_prose = prose_words >= BOT_TABLE_PROSE_MIN_WORDS
        if (
            empty_cell_rows > BOT_EMPTY_CELL_MIN_ROWS
            and dominates
            and not has_heading
            and not has_substantive_prose
        ):
            return True

    return False
