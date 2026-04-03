"""Single source of truth for Claude Code TUI character constants and regexes.

Derived from the Claude Code source (claude-code-main):
- src/components/Spinner/utils.ts — spinner frames
- src/constants/spinnerVerbs.ts — 204 spinner verbs
- src/constants/turnCompletionVerbs.ts — 8 turn completion verbs
- src/constants/figures.ts — response markers, teardrop, pause icon
- src/components/PromptInput/PromptInputModeIndicator.tsx — prompt prefix
- src/components/design-system/Divider.tsx — separator rendering
- src/utils/permissions/PermissionMode.ts — permission mode symbols
- src/components/StatusLine.tsx — status line JSON payload
- src/components/messages/AssistantThinkingMessage.tsx — thinking indicator
- src/components/messages/CompactBoundaryMessage.tsx — compaction marker
- src/components/MessageResponse.tsx — tool result ⎿ prefix

Update this file when Claude Code ships UI changes.
"""

from __future__ import annotations

import re

# ── Prompt and response markers ───────────────────────────────────────

PROMPT_PREFIX = "\u276f"  # ❯ (figures.pointer)

RESPONSE_PREFIXES = (
    "\u23fa",  # ⏺  (macOS/Darwin — BLACK_CIRCLE)
    "\u25cf",  # ●  (Windows/Linux — BLACK_CIRCLE fallback, also reduced-motion spinner)
)

# Tool result prefix — used by MessageResponse component for all tool
# results and special status messages.  Always rendered as "  ⎿  ".
TOOL_RESULT_PREFIX = "\u23bf"  # ⎿ (U+23BF)

# ── Separators and box drawing ────────────────────────────────────────
# Divider.tsx renders ─ (U+2500) across terminal width.
# Title can be embedded: ──── branch-name ──

SEPARATOR_RE = re.compile(r"^\u2500{4,}.*$")

BOX_CHARS = frozenset(
    "\u2500\u2502\u256d\u256e\u256f\u2570"
    "\u2514\u250c\u2510\u2518\u2524\u251c"
    "\u252c\u2534\u253c\u2501\u2503"
)

WELCOME_START = "\u256d"  # ╭ (from borderStyle="round")
WELCOME_END = "\u2570"    # ╰
WELCOME_SIDE = "\u2502"   # │

# ── Status bar and permission modes ───────────────────────────────────
# PermissionMode.ts defines modes: default (no symbol), plan (⏸),
# acceptEdits/bypassPermissions/dontAsk/auto (⏵⏵)

STATUS_BAR_RE = re.compile(r"^\s*(?:\u23f5\u23f5|\u23f8)\s+")  # ⏵⏵ or ⏸

STATUS_TAIL_RE = re.compile(
    r"^\s*\d[\d,]*\s+tokens?\s*$", re.IGNORECASE
)

STATUS_CONTINUATION_RE = re.compile(
    r"^(?:"
    r"\u00b7\s+.*"  # · (middle dot) continuation
    r"|.*(?:"
    r"esc to interrupt"
    r"|shift\+tab to cycle"
    r"|bypass permissions on"
    r"|accept edits on"
    r"|don't ask on"
    r"|auto accept on"
    r"|plan mode on"
    r"|claude install"
    r"|native installer"
    r"|switched from npm"
    r"|update available"
    r").*"
    r")$",
    re.IGNORECASE,
)

# ── Thinking / working indicators ─────────────────────────────────────
# Spinner frames from Spinner/utils.ts: ['·', '✢', '✳', '✶', '✻', '✽']
# ∴ from AssistantThinkingMessage.tsx — represents thinking *block*
#
# Note: · (U+00B7) is excluded from WORKING_PREFIXES because it also
# appears as a bullet point in response content (e.g., "· next steps").
# The spinner cycles at 120ms so · is only visible for one frame.

SPINNER_FRAMES = (
    "\u00b7",  # ·  (frame 0 — ambiguous, also used as bullet)
    "\u2722",  # ✢  (frame 1)
    "\u2733",  # ✳  (frame 2)
    "\u2736",  # ✶  (frame 3)
    "\u273b",  # ✻  (frame 4 / teardrop asterisk)
    "\u273d",  # ✽  (frame 5)
)

# All thinking prefixes (including · for completeness)
THINKING_PREFIXES = (
    "\u2234",  # ∴  (thinking block indicator — AssistantThinkingMessage)
    *SPINNER_FRAMES,
)

# Unambiguous prefixes for working detection — excludes · to avoid
# false positives from bullet points in response content.
WORKING_PREFIXES = (
    "\u2234",  # ∴
    "\u2722",  # ✢
    "\u2733",  # ✳
    "\u2736",  # ✶
    "\u273b",  # ✻
    "\u273d",  # ✽
)

# All 204 spinner verbs from constants/spinnerVerbs.ts (lowercased).
# The TUI renders these as "{Spinner} {Verb}… ({elapsed})"
SPINNER_VERBS = frozenset((
    "accomplishing", "actioning", "actualizing", "architecting",
    "baking", "beaming", "beboppin'", "befuddling", "billowing",
    "blanching", "bloviating", "boogieing", "boondoggling", "booping",
    "bootstrapping", "brewing", "bunning", "burrowing",
    "calculating", "canoodling", "caramelizing", "cascading",
    "catapulting", "cerebrating", "channeling", "channelling",
    "choreographing", "churning", "clauding", "coalescing",
    "cogitating", "combobulating", "composing", "computing",
    "concocting", "considering", "contemplating", "cooking",
    "crafting", "creating", "crunching", "crystallizing", "cultivating",
    "deciphering", "deliberating", "determining", "dilly-dallying",
    "discombobulating", "doing", "doodling", "drizzling",
    "ebbing", "effecting", "elucidating", "embellishing",
    "enchanting", "envisioning", "evaporating",
    "fermenting", "fiddle-faddling", "finagling", "flambéing",
    "flibbertigibbeting", "flowing", "flummoxing", "fluttering",
    "forging", "forming", "frolicking", "frosting",
    "gallivanting", "galloping", "garnishing", "generating",
    "gesticulating", "germinating", "gitifying", "grooving", "gusting",
    "harmonizing", "hashing", "hatching", "herding", "honking",
    "hullaballooing", "hyperspacing",
    "ideating", "imagining", "improvising", "incubating",
    "inferring", "infusing", "ionizing",
    "jitterbugging", "julienning",
    "kneading",
    "leavening", "levitating", "lollygagging",
    "manifesting", "marinating", "meandering", "metamorphosing",
    "misting", "moonwalking", "moseying", "mulling", "mustering", "musing",
    "nebulizing", "nesting", "newspapering", "noodling", "nucleating",
    "orbiting", "orchestrating", "osmosing",
    "perambulating", "percolating", "perusing", "philosophising",
    "photosynthesizing", "pollinating", "pondering", "pontificating",
    "pouncing", "precipitating", "prestidigitating", "processing",
    "proofing", "propagating", "puttering", "puzzling",
    "quantumizing",
    "razzle-dazzling", "razzmatazzing", "recombobulating",
    "reticulating", "roosting", "ruminating",
    "sautéing", "scampering", "schlepping", "scurrying", "seasoning",
    "shenaniganing", "shimmying", "simmering", "skedaddling",
    "sketching", "slithering", "smooshing", "sock-hopping",
    "spelunking", "spinning", "sprouting", "stewing", "sublimating",
    "swirling", "swooping", "symbioting", "synthesizing",
    "tempering", "thinking", "thundering", "tinkering",
    "tomfoolering", "topsy-turvying", "transfiguring",
    "transmuting", "twisting",
    "undulating", "unfurling", "unravelling",
    "vibing",
    "waddling", "wandering", "warping", "whatchamacalliting",
    "whirlpooling", "whirring", "whisking", "wibbling",
    "working", "wrangling",
    "zesting", "zigzagging",
))

# Backward compat alias
THINKING_VERBS = SPINNER_VERBS

# Turn completion verbs from constants/turnCompletionVerbs.ts.
# These are past-tense and appear as "✻ {Verb} for {duration}".
TURN_COMPLETION_VERBS = frozenset((
    "baked", "brewed", "churned", "cogitated",
    "cooked", "crunched", "sautéed", "worked",
))

# ── Compaction ────────────────────────────────────────────────────────
# CompactBoundaryMessage.tsx: "✻ Conversation compacted (ctrl+o for history)"

COMPACTION_RE = re.compile(
    r"^\u273b\s+(?:Conversation compacted|Churned)",
)

# ── Feedback survey ───────────────────────────────────────────────────
# FeedbackSurveyView.tsx: "How is Claude doing this session?"

FEEDBACK_RE = re.compile(r"how is claude doing", re.IGNORECASE)

# ── Noise prefixes (TUI chrome, not content) ──────────────────────────

NOISE_PREFIXES = (
    WELCOME_START,   # ╭ welcome box top
    WELCOME_END,     # ╰ welcome box bottom
    WELCOME_SIDE,    # │ welcome box side
    "\u23f5\u23f5",  # ⏵⏵ status bar
    "\u23f8",        # ⏸  plan mode status bar
)

# Custom status line (from ~/.claude/statusline-command.sh)
STATUS_LINE_RE = re.compile(
    r"^\s*sTAT\s*\|",
)

# ── Shell prompt markers (post-exit detection) ────────────────────────

SHELL_PROMPT_CHARS = ("$", "%", "#", "❯")
