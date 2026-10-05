"""Predictive analytics agent with Docker sandbox for code execution.

Main agent has 3 tools:
- query_data: Read and filter sales data from JSON
- predict: Sub-agent executes Python (sklearn/pandas) in Docker
- generate_chart: Returns structured LineChartData for frontend rendering
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pydantic_ai import Agent, RunContext

from pydantic_ai_backends import create_console_toolset, get_console_system_prompt

from .models import AnalyticsDeps, ChartSeries, DataPoint, LineChartData

CHART_DATA_PREFIX = "CHART_DATA:"

analytics_agent: Agent[AnalyticsDeps, str] = Agent(
    "openai:gpt-4.1",
    deps_type=AnalyticsDeps,
)


@analytics_agent.system_prompt
async def _system_prompt() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""\
You are a predictive analytics assistant. You help users explore sales data, \
run predictions, and visualize results.

Current date/time: {now}

You have access to monthly sales data for three products (Widget Alpha, Widget Beta, \
Widget Gamma) across three regions (North, South, West), spanning 2024-01 to 2025-12. \
The LAST month in the dataset is 2025-12. Any forecast for "the next N months" must \
start from 2026-01 and go forward (2026-01, 2026-02, ...). Never predict within the \
historical range — predictions must EXTEND beyond the data.

## Available tools

- **query_data** — Read and filter the sales dataset. Use this first to understand the data.
- **predict** — Run Python code (sklearn, pandas, numpy) in an isolated Docker sandbox. \
A sub-agent writes and executes the prediction script for you. Provide a clear description \
of what to predict and how.
- **generate_chart** — Create a line chart. Returns structured data rendered on the frontend. \
Pass the data as a JSON array of series.

## Typical workflow

1. Use query_data to explore the data
2. Use predict to run forecasting or trend analysis
3. Use generate_chart to visualize the results

## Important rules

- The generate_chart tool renders charts on the frontend automatically. \
Do NOT write markdown image syntax like ![title](CHART_DATA). Just call the tool.
- When charting predictions, ALWAYS include historical data alongside forecast data \
as separate series (e.g. "Historical" and "Forecast") so the user can see the trend in context.
- For predictions, ask the sub-agent to use polynomial regression (degree 2-3) or \
Holt-Winters smoothing. NEVER request ARIMA or SARIMA — the dataset is too small (24 points) \
and these models produce absurd exponential forecasts. Stick to simple, stable models.

Always explain your findings to the user. When you have numeric results from predictions, \
visualize them with generate_chart.\
"""


@analytics_agent.tool
async def query_data(
    ctx: RunContext[AnalyticsDeps],
    product: str | None = None,
    region: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    aggregation: str | None = None,
) -> str:
    """Query the sales dataset with optional filters and aggregation.

    Args:
        product: Filter by product name (e.g. 'Widget Alpha'). None for all products.
        region: Filter by region ('North', 'South', 'West'). None for all regions.
        start_date: Start date filter inclusive (YYYY-MM format). None for no lower bound.
        end_date: End date filter inclusive (YYYY-MM format). None for no upper bound.
        aggregation: Aggregation mode. One of:
            - 'monthly_by_product': Sum units/revenue per (date, product)
            - 'monthly_by_region': Sum units/revenue per (date, region)
            - 'total_by_product': Total units/revenue per product
            - None: Return raw records
    """
    data_path = Path(ctx.deps.data_path)
    with open(data_path) as f:
        data = json.load(f)

    records = data["records"]

    if product:
        records = [r for r in records if r["product"] == product]
    if region:
        records = [r for r in records if r["region"] == region]
    if start_date:
        records = [r for r in records if r["date"] >= start_date]
    if end_date:
        records = [r for r in records if r["date"] <= end_date]

    if not records:
        return "No records found matching the filters."

    if aggregation == "monthly_by_product":
        groups: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: {"units_sold": 0, "revenue": 0.0}
        )
        for r in records:
            key = (r["date"], r["product"])
            groups[key]["units_sold"] += r["units_sold"]
            groups[key]["revenue"] += r["revenue"]
        result = [{"date": k[0], "product": k[1], **v} for k, v in sorted(groups.items())]
    elif aggregation == "monthly_by_region":
        groups = defaultdict(lambda: {"units_sold": 0, "revenue": 0.0})
        for r in records:
            key = (r["date"], r["region"])
            groups[key]["units_sold"] += r["units_sold"]
            groups[key]["revenue"] += r["revenue"]
        result = [{"date": k[0], "region": k[1], **v} for k, v in sorted(groups.items())]
    elif aggregation == "total_by_product":
        groups = defaultdict(lambda: {"units_sold": 0, "revenue": 0.0})
        for r in records:
            groups[r["product"]]["units_sold"] += r["units_sold"]
            groups[r["product"]]["revenue"] += r["revenue"]
        result = [{"product": k, **v} for k, v in sorted(groups.items())]
    else:
        result = records

    output = json.dumps(result[:100], indent=2)
    return f"Found {len(result)} records.\n\n{output}"


@analytics_agent.tool
async def predict(
    ctx: RunContext[AnalyticsDeps],
    task_description: str,
) -> str:
    """Run a prediction using Python (sklearn, pandas, numpy) in a Docker sandbox.

    A sub-agent writes and executes Python code to perform the prediction.
    The sales data is available at /workspace/sales_data.json inside the sandbox.

    Args:
        task_description: Clear description of the prediction task.
            Example: 'Predict Widget Alpha units_sold for the next 6 months
            using linear regression on monthly totals across all regions'
    """
    workspace = ctx.deps.workspace

    # Write sales data into the Docker container
    data_path = Path(ctx.deps.data_path)
    await workspace.write_text("/workspace/sales_data.json", data_path.read_text())

    console_toolset = create_console_toolset(
        include_execute=True,
        require_write_approval=False,
        require_execute_approval=False,
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sub_agent: Agent[None, str] = Agent(
        "openai:gpt-4.1",
        system_prompt=f"""\
You are a data science code executor. You have a Python environment with pandas, \
numpy, scikit-learn, matplotlib, and seaborn installed.

Current date/time: {now}

{get_console_system_prompt()}

The sales data is at /workspace/sales_data.json. It contains monthly records with \
columns: date, product, units_sold, revenue, region. \
The data spans 2024-01 to 2025-12 (the LAST data point is December 2025).

Your task: write a Python script to perform the requested prediction, execute it, \
and return the results.

Steps:
1. Write a Python script to /workspace/predict.py
2. Execute it with: python /workspace/predict.py
3. Return the prediction results from stdout

CRITICAL:
- When predicting "the next N months", forecast dates MUST start from 2026-01 onward. \
The historical data ends at 2025-12. Never output predictions for dates already in the dataset.
- Print results to stdout in a clear, structured format (numbers, tables, JSON)
- Do NOT use matplotlib.show() or any display — just print numeric output
- If generating future predictions, print them as JSON for easy parsing \
with dates like 2026-01, 2026-02, etc.

MODEL SELECTION — VERY IMPORTANT:
- The dataset is SMALL (24 monthly data points). Do NOT use complex models like ARIMA, \
SARIMA, or VAR — they will overfit and produce absurd exponential forecasts.
- Preferred models (in order): Linear Regression, Polynomial Regression (degree 2-3 max), \
Holt-Winters / Simple Exponential Smoothing (from sklearn or manual), or Random Forest.
- NEVER use statsmodels ARIMA/SARIMAX on this data — it WILL explode.

SANITY CHECK — MANDATORY:
- After computing predictions, check that ALL predicted values are within a reasonable \
range: between 0 and 3x the historical maximum for that metric.
- If any prediction exceeds this range, fall back to simple Linear Regression.
- Print a warning if fallback was triggered.\
""",
        toolsets=[console_toolset],
    )

    # The sub-agent works in the same container: `workspace=` hands it over.
    result = await sub_agent.run(
        f"Perform this prediction task:\n\n{task_description}",
        workspace=workspace,
    )

    return result.output


@analytics_agent.tool
async def generate_chart(
    ctx: RunContext[AnalyticsDeps],
    title: str,
    x_label: str,
    y_label: str,
    series_json: str,
) -> str:
    """Generate a line chart displayed on the frontend.

    Args:
        title: Chart title (e.g. 'Monthly Sales Forecast').
        x_label: X-axis label (e.g. 'Month').
        y_label: Y-axis label (e.g. 'Units Sold').
        series_json: JSON array of series. Format:
            [{"name": "Widget Alpha", "data_points": [{"x": "2024-01", "y": 120}, ...]}]
    """
    series_data = json.loads(series_json)

    chart = LineChartData(
        title=title,
        x_label=x_label,
        y_label=y_label,
        series=[
            ChartSeries(
                name=s["name"],
                data_points=[DataPoint(x=dp["x"], y=dp["y"]) for dp in s["data_points"]],
            )
            for s in series_data
        ],
    )

    return f"{CHART_DATA_PREFIX}{chart.model_dump_json()}"
