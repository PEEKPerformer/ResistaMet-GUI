// A static X-Y plot: several series of (x, y) point sets, redrawn when the
// data prop changes. For results that arrive whole — a sweep — rather than a
// stream; LivePlot is the streaming counterpart.

import { useEffect, useRef } from "react";
import uPlot, { type AlignedData, type Options } from "uplot";
import "uplot/dist/uPlot.min.css";
import { axisLabels } from "../../lib/format";
import styles from "./LivePlot.module.css";

export interface XYSeries {
  label: string;
  color: string;
  x: number[];
  y: number[];
  /** Draw markers on points as well as the line. */
  points?: boolean;
}

interface Props {
  series: XYSeries[];
  xUnit: string;
  yUnit: string;
  xLabel?: string;
  yLabel?: string;
}

export function XYPlot({ series, xUnit, yUnit }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const computed = getComputedStyle(host);
    const resolve = (color: string) => {
      const match = /^var\((--[\w-]+)\)$/.exec(color.trim());
      return match ? computed.getPropertyValue(match[1]!).trim() || "#888" : color;
    };
    const axisText = computed.getPropertyValue("--axis-text").trim() || "#888";
    const gridLine = computed.getPropertyValue("--grid-line").trim() || "rgba(255,255,255,.08)";
    const font = "11px " + computed.getPropertyValue("--font-mono");

    const axisValues = (unit: string) => (_u: uPlot, ticks: number[]) => axisLabels(ticks, unit);

    // uPlot wants one shared x array. Series with different x sets are
    // merged onto the union of x values, with nulls where a series has none.
    const xs = Array.from(new Set(series.flatMap((s) => s.x))).sort((a, b) => a - b);
    const index = new Map(xs.map((x, i) => [x, i]));
    const columns = series.map((s) => {
      const column = new Array<number | null>(xs.length).fill(null);
      s.x.forEach((x, i) => {
        const at = index.get(x);
        if (at !== undefined) column[at] = s.y[i] ?? null;
      });
      return column;
    });

    const options: Options = {
      width: host.clientWidth,
      height: host.clientHeight,
      padding: [12, 16, 0, 8],
      cursor: { drag: { x: true, y: true }, focus: { prox: 16 } },
      legend: { show: false },
      scales: { x: { time: false }, y: { auto: true } },
      axes: [
        { stroke: axisText, grid: { stroke: gridLine, width: 1 }, ticks: { stroke: gridLine, width: 1 }, font, values: axisValues(xUnit), size: 30 },
        { stroke: axisText, grid: { stroke: gridLine, width: 1 }, ticks: { stroke: gridLine, width: 1 }, font, values: axisValues(yUnit), size: 64 },
      ],
      series: [
        {},
        ...series.map((s) => ({
          label: s.label,
          stroke: resolve(s.color),
          width: 1.5,
          points: { show: s.points ?? true, size: 5, fill: resolve(s.color) },
          spanGaps: true,
        })),
      ],
    };

    const plot = new uPlot(options, [xs, ...columns] as AlignedData, host);
    const observer = new ResizeObserver(() => plot.setSize({ width: host.clientWidth, height: host.clientHeight }));
    observer.observe(host);
    return () => {
      observer.disconnect();
      plot.destroy();
    };
  }, [series, xUnit, yUnit]);

  return <div className={styles.host} ref={hostRef} />;
}
