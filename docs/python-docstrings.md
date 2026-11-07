# Python docstring standard

Follow [PEP 257](https://peps.python.org/pep-0257/) with Google-style sections
only when a summary needs more detail.

- Document every module, class, function, and method, including constructors,
  nested MCP tools, and tests. Describe the module's purpose, the class's role,
  or the callable's observable behavior.
- Use triple double quotes as the first statement, after any shebang or script
  metadata for modules and before any imports, including `__future__` imports.
- Start with one short, capitalized sentence ending in a period. Use the
  imperative for callables: "Return", "Validate", "Run", or "Verify".
- Prefer a single line with both quotes on that line. Do not repeat the
  signature, type annotations, implementation steps, or the function name.
- When needed, follow the summary with a blank line and explain constraints,
  side effects, failure behavior, or return semantics such as `None` on failure.
  Add `Args:`, `Returns:`, `Yields:`, or `Raises:` only when they add useful
  contract details; omit empty sections and repeated types. Indent entries by
  four spaces and place the closing quotes on their own line.
- Leave a blank line after class docstrings. Keep function bodies directly
  below their docstrings and separate module docstrings from imports by a blank line.
- MCP tool docstrings become client-visible descriptions. State the tool's
  purpose and limits accurately, and preserve that contract during docstring edits.

```python
def health() -> HealthResponse:
    """Report server liveness and the running application version."""
    return HealthResponse()
```

Review docstrings alongside the implementation. A docstring-only refactor must
preserve signatures and executable statements; use existing behavior tests
and MCP calls to verify it. Do not add tests that pin documentation wording.
