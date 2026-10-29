#!/usr/bin/env python3
"""Render the paper's result tables in the README and interactive report."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "<!-- BEGIN PAPER RESULTS -->"
END = "<!-- END PAPER RESULTS -->"
BANDS = ("easy", "medium", "hard", "overall")


def write(path: Path, text: str) -> None:
    if not path.exists() or path.read_text() != text:
        path.write_text(text, encoding="utf-8")


def replace_block(path: Path, content: str) -> None:
    text = path.read_text()
    before, rest = text.split(BEGIN, 1)
    _, after = rest.split(END, 1)
    write(path, before + BEGIN + "\n" + content.strip() + "\n" + END + after)


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    return "\n".join(
        ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" if header in ("Harness", "Model", "Method") else "---:" for header in headers) + " |"]
        + ["| " + " | ".join(row) + " |" for row in rows]
    )


def method_label(row: dict, document: Path, *, as_html: bool = False) -> str:
    label = row["method"]
    if "source" not in row:
        return html.escape(label) if as_html else label
    source = ROOT / row["source"]
    if not source.is_file():
        raise FileNotFoundError(source)
    link = os.path.relpath(source, document.parent)
    if as_html:
        return f'<a href="{html.escape(link, quote=True)}">{html.escape(label)}</a>'
    return f"[{label}]({link})"


def case_rows(kind: str, data: dict, document: Path, *, as_html: bool = False) -> list[list[str]]:
    if kind == "datacenter":
        return [[method_label(row, document, as_html=as_html), f'{row["peak_gbps"]:,.3f}', f'{row["migration_pb"]:.3f}'] for row in data["rows"]]
    return [[method_label(row, document, as_html=as_html)] + [f'{row[key]:.3f}' for key in ("total_million_usd_per_year", "investment_million_usd_per_year", "operation_million_usd_per_year")] for row in data["rows"]]


def case_description(kind: str, data: dict) -> str:
    if kind == "datacenter":
        return f'VERAS reduces peak inter-datacenter bandwidth by **{data["peak_reduction_percent"]:.2f}%** compared with Native Codex.'
    selected = next(row for row in data["rows"] if row["method"] == "VERAS")
    cost = selected["total_million_usd_per_year"]
    return f'VERAS achieves an annualized cost of **USD {cost:.3f}M/year**, reducing cost by **{data["hourly_model_cost_reduction_percent"]:.2f}%** relative to Hourly MISOCP and **{data["native_codex_cost_reduction_percent"]:.2f}%** relative to Native Codex.'


def benchmark_markdown(data: dict) -> str:
    rows = []
    previous = None
    for row in data["rows"]:
        harness = row["harness"] if row["harness"] != previous else ""
        previous = row["harness"]
        values = [harness, row["model"], f'{row["feasibility"]["overall"]:.2f}', f'{row["quality"]["overall"]:.2f}']
        values += [f'{row["pass_rate_percent"][band]:.2f}%' for band in BANDS]
        if row["harness"] == "VERAS":
            values = [f"**{value}**" for value in values]
        rows.append(values)
    return markdown_table(["Harness", "Model", "Overall feasibility", "Overall quality", "Easy pass", "Medium pass", "Hard pass", "Overall pass"], rows)


def benchmark_html(data: dict) -> str:
    heads = '<thead><tr><th rowspan="2">Harness</th><th rowspan="2">Model</th><th colspan="4">Feasibility ↑</th><th colspan="4">Quality ↑</th><th colspan="4">Pass rate (%) ↑</th></tr><tr>'
    heads += "".join(f"<th>{band.title()}</th>" for _ in range(3) for band in BANDS) + "</tr></thead>"
    rows = []
    previous = None
    for index, row in enumerate(data["rows"]):
        featured = ' class="featured-row"' if row["harness"] == "VERAS" else ""
        cells = []
        if row["harness"] != previous:
            span = 1
            for following in data["rows"][index + 1:]:
                if following["harness"] != row["harness"]:
                    break
                span += 1
            cells.append(f'<td rowspan="{span}">{html.escape(row["harness"])}</td>')
        previous = row["harness"]
        cells.append(f'<td>{html.escape(row["model"])}</td>')
        for metric in ("feasibility", "quality", "pass_rate_percent"):
            cells.extend(f'<td>{row[metric][band]:.2f}</td>' for band in BANDS)
        rows.append(f"<tr{featured}>" + "".join(cells) + "</tr>")
    return '<table class="baseline" id="benchmark-comparison" data-paper-table="1">' + heads + "<tbody>" + "\n".join(rows) + "</tbody></table>"


def case_html(kind: str, data: dict, document: Path) -> str:
    headers = ["Method", "Peak (Gbps) ↓", "Migration (PB) ↓"] if kind == "datacenter" else ["Method", "Total ↓", "Investment ↓", "Operation ↓"]
    number = 2 if kind == "datacenter" else 3
    rows = []
    for row, values in zip(data["rows"], case_rows(kind, data, document, as_html=True)):
        featured = ' class="featured-row"' if row["method"] == "VERAS" else ""
        rows.append(f"<tr{featured}>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>")
    return f'<table class="baseline case-table" id="{kind}-comparison" data-paper-table="{number}"><thead><tr>' + "".join(f"<th>{header}</th>" for header in headers) + "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"


def main() -> None:
    data = json.loads((ROOT / "veras/evidence/paper-results.json").read_text())
    document = ROOT / "README.md"
    selected = next(row for row in data["benchmark"]["rows"] if row["harness"] == "VERAS")
    baseline = next(row for row in data["benchmark"]["rows"] if row["harness"] == "Codex" and row["model"] == selected["model"])
    sections = [
        "## ORAgentBench results\n\n"
        "Results from Table 1 of the paper, covering 107 tasks: 32 Easy, 41 Medium, and 34 Hard.\n"
        "A task passes when feasibility is positive and normalized quality exceeds `0.4`.\n\n"
        + benchmark_markdown(data["benchmark"])
        + f'\n\nVERAS achieves **{selected["pass_rate_percent"]["overall"]:.2f}%** overall pass rate compared with **{baseline["pass_rate_percent"]["overall"]:.2f}%** for Codex with {selected["model"]}.\n'
        + f'Hard-task pass rate increases from **{baseline["pass_rate_percent"]["hard"]:.2f}%** to **{selected["pass_rate_percent"]["hard"]:.2f}%**.',
        "## Industrial case studies",
    ]
    for kind, title, headers, location in [
        ("datacenter", "Inter-datacenter placement", ["Method", "Peak (Gbps) ↓", "Migration (PB) ↓"], "data_center"),
        ("power_grid", "54-bus distribution network planning", ["Method", "Total ↓", "Investment ↓", "Operation ↓"], "power_grid"),
    ]:
        unit = "Peak bandwidth and migration volume." if kind == "datacenter" else "Annualized costs in million USD/year."
        sections.append(f"### {title}\n\n{case_description(kind, data[kind])}\n\n{unit}\n\n" + markdown_table(headers, case_rows(kind, data[kind], document)))
        case_document = ROOT / f"real_world/{location}/README.md"
        runtime = "The dispatchers read task inputs under `/app/data` and generate a placement Parquet file under `/app/submissions`." if kind == "datacenter" else "Run the generated solvers in the task's Python environment with the 54-bus input workbook. The solvers write their NPZ solution under `/app/submissions`."
        case_text = f"# {title}\n\n{case_description(kind, data[kind])}\n\n{unit}\n\n" + markdown_table(headers, case_rows(kind, data[kind], case_document))
        case_text += f"\n\n## Running the algorithms\n\n{runtime}\n\nSee `../../veras/README.md` for model and runtime configuration.\n"
        write(case_document, case_text)
    replace_block(document, "\n\n".join(sections))

    report = ROOT / "veras/reports/benchmark-results/index.html"
    blocks = ['<section id="benchmark-results"><div class="section-head"><div><div class="eyebrow">Paper · Table 1</div><h2>ORAgentBench comparison</h2></div><p>Feasibility, normalized quality, and pass rates across 107 tasks.</p></div><div class="table-wrap">' + benchmark_html(data["benchmark"]) + "</div></section>"]
    for kind, title, unit in [("datacenter", "Inter-datacenter placement", "Peak bandwidth in Gbps; migration volume in PB."), ("power_grid", "54-bus network planning", "Annualized costs in million USD/year.")]:
        number = 2 if kind == "datacenter" else 3
        description = case_description(kind, data[kind]).replace("**", "")
        blocks.append(f'<section id="{kind}-results"><div class="section-head"><div><div class="eyebrow">Paper · Table {number}</div><h2>{title}</h2></div><p>{unit}</p></div><div class="table-wrap">' + case_html(kind, data[kind], report) + f'</div><p>{description}</p></section>')
    replace_block(report, "\n\n".join(blocks))

    overview = "# Industrial Case Studies\n\n" + case_description("datacenter", data["datacenter"]) + "\n\n" + case_description("power_grid", data["power_grid"])
    overview += "\n\n- [Inter-datacenter placement](data_center/README.md): algorithms, peak bandwidth, and migration results.\n- [54-bus distribution network planning](power_grid/README.md): solvers and annualized cost results.\n"
    write(ROOT / "real_world/README.md", overview)
    print("Rendered paper-aligned README, case-study documentation, and HTML tables.")


if __name__ == "__main__":
    main()
