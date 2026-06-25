from __future__ import annotations


def _cell(value) -> str:
    text = "" if value is None else str(value)
    if text == "nan":
        text = ""
    return text.replace("\n", " ").replace("|", "\\|")


def df_to_markdown(df, *, index: bool = False) -> str:
    """Render a DataFrame as Markdown without requiring tabulate.

    Pandas delegates to the optional tabulate package. Keep that fast path when
    available, but provide a small fallback so static rendering works in lean
    environments.
    """

    try:
        return df.to_markdown(index=index)
    except ImportError:
        pass

    data = df.reset_index() if index else df.copy()
    columns = [str(c) for c in data.columns]
    rows = [[_cell(row[c]) for c in data.columns] for _, row in data.iterrows()]
    widths = [len(c) for c in columns]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))

    def fmt(row: list[str]) -> str:
        return "| " + " | ".join(value.ljust(widths[i]) for i, value in enumerate(row)) + " |"

    header = fmt(columns)
    sep = "| " + " | ".join("-" * max(width, 3) for width in widths) + " |"
    return "\n".join([header, sep, *[fmt(row) for row in rows]])
