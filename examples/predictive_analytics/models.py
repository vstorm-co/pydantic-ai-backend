"""Pydantic models for the predictive analytics demo."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai.workspaces import Workspace


class DataPoint(BaseModel):
    """A single x/y data point for a chart series."""

    x: str = Field(description="X-axis value (e.g. date '2024-01')")
    y: float = Field(description="Y-axis numeric value")


class ChartSeries(BaseModel):
    """A named series of data points."""

    name: str = Field(description="Legend name for this series")
    data_points: list[DataPoint] = Field(description="Ordered data points")


class LineChartData(BaseModel):
    """Structured chart data rendered by the frontend with Chart.js."""

    title: str = Field(description="Chart title")
    x_label: str = Field(description="X-axis label")
    y_label: str = Field(description="Y-axis label")
    series: list[ChartSeries] = Field(description="Data series to plot")


@dataclass
class AnalyticsDeps:
    """Dependencies for the analytics agent."""

    workspace: Workspace
    """The container the prediction sub-agent writes and runs code in."""
    data_path: str
