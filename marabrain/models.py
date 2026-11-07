"""Validated input contracts for the Crystalline-backed memory tools."""

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

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


Domain = Annotated[str, Field(min_length=1, max_length=128, description="Registered domain name."), AfterValidator(_positional)]
Permalink = Annotated[str, Field(min_length=1, max_length=512, description="Domain-relative permalink, as returned by search."), AfterValidator(_relative_path)]
Title = Annotated[str, Field(min_length=1, max_length=256, description="Memory title."), AfterValidator(_positional)]
Folder = Annotated[str, Field(min_length=1, max_length=512, description="Domain-relative subfolder."), AfterValidator(_relative_path)]
Tag = Annotated[str, Field(min_length=1, max_length=64, description="Tag without commas."), AfterValidator(_tag)]
Tags = Annotated[list[Tag], Field(max_length=32)]
Domains = Annotated[list[Domain], Field(min_length=1, description="Domains to search; at least one.")]
Content = Annotated[str, AfterValidator(_text)]
Section = Annotated[str, Field(min_length=1, max_length=512, description="Heading path of the target section."), AfterValidator(_single_line)]
Checksum = Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,128}$", description="Checksum from a previous read.")]
Query = Annotated[str, Field(min_length=1, max_length=1024, description="Search text."), AfterValidator(_positional)]
Limit = Annotated[int, Field(ge=1, le=100)]
Page = Annotated[int, Field(ge=1)]


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


EditOperation = Annotated[
    AppendEdit | PrependEdit | FindReplaceEdit | ReplaceSectionEdit | InsertBeforeSectionEdit | InsertAfterSectionEdit,
    Field(discriminator="type"),
]
