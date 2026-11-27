# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Summarize the dependency updates in a Renovate pull request."""

from tbhbdev.alpha.merge.body import MD_LINK


def table_cells(row: str) -> list[str]:
    """The cells of one Markdown table row, stripped."""
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def plain_cell(cell: str) -> str:
    """A table cell as plain text: link labels kept, code backticks dropped."""
    return MD_LINK.sub(r"\2", cell).replace("`", "")


def renovate_body(body: str) -> str:
    """One list item per update from a Renovate PR body's update table.

    Renovate's body opens with a ``| Package | ... | Change |`` table, then
    release notes and its own configuration notes, which don't belong in a
    commit message. Only the table survives, as ``- name change (update)``.
    A body without that table gives none.
    """
    lines = body.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith("|")
            and table_cells(line)[0].lower() == "package"
        ),
        None,
    )
    if start is None:
        return ""
    header = [cell.lower() for cell in table_cells(lines[start])]
    items: list[str] = []
    for line in lines[start + 2 :]:
        if not line.strip().startswith("|"):
            break
        row = dict(zip(header, map(plain_cell, table_cells(line)), strict=False))
        update = f" ({row['update']})" if row.get("update") else ""
        items.append(f"- {row['package']} {row.get('change', '')}".rstrip() + update)
    # Renovate lists one dependency once per manager that found it.
    unique = list(dict.fromkeys(items))
    return "\n".join(unique) + "\n" if unique else ""
