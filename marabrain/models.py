"""Validated input contracts for the Crystalline-backed memory tools."""

import json
import re
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _text(value: str) -> str:
    """Reject NUL, which cannot be passed to a subprocess."""
    if "\x00" in value:
        raise ValueError("must not contain NUL")
    return value


def _single_line(value: str) -> str:
    """Reject control characters and surrounding whitespace."""
    if _CONTROL.search(value) or value != value.strip():
        raise ValueError("must be a single line without surrounding whitespace")
    return value


def _positional(value: str) -> str:
    """Reject values that a command line could parse as an option."""
    if _single_line(value).startswith("-"):
        raise ValueError("must not start with a hyphen")
    return value


def _relative_path(value: str) -> str:
    """Require a slash-separated relative path without empty, dot, or dot-dot segments."""
    _positional(value)
    if "\\" in value or "://" in value or any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError("must be a canonical relative path")
    return value


def _tag(value: str) -> str:
    """Reject tags that would be ambiguous in a comma-separated list."""
    if "," in _single_line(value):
        raise ValueError("must not contain a comma")
    return value


def _is_date(value: str) -> bool:
    """Return whether the text names a real calendar date."""
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _metadata(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Require finite JSON numbers and valid Unicode before executing the CLI."""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeEncodeError) as error:
        raise ValueError("metadata must contain valid JSON and Unicode") from error
    return value


WorkspaceId = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
        description="Case-sensitive workspace ID authorized by the trusted platform; 1-64 ASCII alphanumerics, underscores, or hyphens, starting alphanumeric.",
    ),
]
Domain = Annotated[str, Field(min_length=1, max_length=128, description="Registered domain name."), AfterValidator(_positional)]
Permalink = Annotated[str, Field(min_length=1, max_length=512, description="Domain-relative permalink, as returned by search."), AfterValidator(_relative_path)]
Title = Annotated[str, Field(min_length=1, max_length=256, description="Memory title."), AfterValidator(_positional)]
Folder = Annotated[str, Field(min_length=1, max_length=512, description="Domain-relative subfolder."), AfterValidator(_relative_path)]
Tag = Annotated[str, Field(min_length=1, max_length=64, description="Tag without commas."), AfterValidator(_tag)]
Tags = Annotated[list[Tag], Field(max_length=32)]
Domains = Annotated[list[Domain], Field(min_length=1, description="Domains to search; at least one.")]
Content = Annotated[str, AfterValidator(_text)]
Metadata = Annotated[dict[str, JsonValue], Field(description="JSON object with additional memory metadata."), AfterValidator(_metadata)]
Section = Annotated[str, Field(min_length=1, max_length=512, description="Heading path of the target section."), AfterValidator(_single_line)]
Checksum = Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,128}$", description="Checksum from a previous read.")]
Query = Annotated[str, Field(min_length=1, max_length=1024, description="Search text."), AfterValidator(_positional)]
Limit = Annotated[int, Field(ge=1, le=100)]
Page = Annotated[int, Field(ge=1)]
Status = Annotated[
    str, Field(min_length=1, max_length=64, description="Lifecycle status, such as stable, superseded, or deprecated."), AfterValidator(_positional)
]
Depth = Annotated[int, Field(ge=1, le=3, description="Relation hops to traverse from the anchor.")]
MaxRelated = Annotated[int, Field(ge=1, le=100, description="Keep only this many top-ranked related memories beyond the anchor.")]
Timeframe = Annotated[
    str, Field(pattern=r"^[1-9][0-9]{0,3}[dwy]$", description="Keep only related memories recorded within this window: days, weeks, or years, such as 7d.")
]
Family = Literal["temporal", "structure", "redundancy", "meaning"]
Families = Annotated[list[Family], Field(min_length=1, max_length=4, description="Detector families to restrict the review to.")]
FrontmatterKey = Literal[
    "tags", "status", "valid_from", "valid_to", "stale_after", "source_date", "resource", "source_version", "sources", "salience", "verified", "evolve_ack"
]
_LIST_KEYS = frozenset({"tags", "sources"})
_DATE_KEYS = frozenset({"valid_from", "valid_to", "stale_after", "source_date"})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FrontmatterItem = Annotated[str, Field(min_length=1, max_length=512), AfterValidator(_tag)]
FrontmatterValue = Annotated[
    str, Field(min_length=1, max_length=2048, description="New scalar value for keys other than tags and sources."), AfterValidator(_single_line)
]
FrontmatterValues = Annotated[list[FrontmatterItem], Field(max_length=64, description="Whole new list for tags or sources; an empty list removes it.")]


class _Edit(BaseModel):
    """Immutable base for one edit operation carrying the new content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: Content = Field(description="Text to insert or substitute; a section body never repeats its heading.")


class AppendEdit(_Edit):
    """Append content to the end of the memory."""

    type: Literal["append"]


class PrependEdit(_Edit):
    """Prepend content to the start of the memory body."""

    type: Literal["prepend"]


class FindReplaceEdit(_Edit):
    """Replace occurrences of exact text with the content."""

    type: Literal["find_replace"]
    find_text: Annotated[str, Field(min_length=1, description="Exact text to find."), AfterValidator(_text)]
    expected_replacements: Annotated[int, Field(ge=0, description="Exact number of replacements required.")] | None = None


class ReplaceSectionEdit(_Edit):
    """Replace a section body, optionally including its subsections."""

    type: Literal["replace_section"]
    section: Section
    include_subsections: bool = False


class InsertBeforeSectionEdit(_Edit):
    """Insert content before a section."""

    type: Literal["insert_before_section"]
    section: Section


class InsertAfterSectionEdit(_Edit):
    """Insert content after a section."""

    type: Literal["insert_after_section"]
    section: Section


class SetFrontmatterEdit(BaseModel):
    """Assign or remove one frontmatter field; omitting both value and values removes the field (verified stamps the caller)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["set_frontmatter"]
    key: FrontmatterKey
    value: FrontmatterValue | None = None
    values: FrontmatterValues | None = None

    @model_validator(mode="after")
    def _check_value(self) -> Self:
        """Match the value form to the key and validate dates, salience, and status before execution."""
        if self.key in _LIST_KEYS:
            if self.value is not None:
                raise ValueError(f"{self.key} takes values, not value")
        elif self.values is not None:
            raise ValueError(f"{self.key} takes value, not values")
        elif self.value is not None:
            if self.key in _DATE_KEYS:
                if not _DATE.fullmatch(self.value) or not _is_date(self.value):
                    raise ValueError(f"{self.key} must be a YYYY-MM-DD date")
            elif self.key == "salience":
                try:
                    salience = float(self.value)
                except ValueError:
                    raise ValueError("salience must be a number from 0 to 10") from None
                if not 0 <= salience <= 10:
                    raise ValueError("salience must be a number from 0 to 10")
            elif self.key == "status":
                _positional(self.value)
                if len(self.value) > 64:
                    raise ValueError("status must be at most 64 characters")
        return self


EditOperation = Annotated[
    AppendEdit | PrependEdit | FindReplaceEdit | ReplaceSectionEdit | InsertBeforeSectionEdit | InsertAfterSectionEdit | SetFrontmatterEdit,
    Field(discriminator="type"),
]
