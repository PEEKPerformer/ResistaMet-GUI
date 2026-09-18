// A live time-series plot on uPlot.
//
// uPlot draws hundreds of thousands of points at 60 fps because it does very
// little: it owns a canvas, we hand it arrays. This wrapper does the two things
// React would otherwise get wrong — it never re-renders on data changes (a
// requestAnimationFrame loop pulls the sample buffers directly), and it
// resizes with its container instead of a fixed width.

import { useEffect, useRef } from "react";
import uPlot, { type AlignedData, type Options, type Series } from "uplot";
import "uplot/dist/uPlot.min.css";
import { getSeries, getSamplesVersion } from "../../state/samples";
import { engineering } from "../../lib/format";
import styles from "./LivePlot.module.css";

export interface TraceSpec {
  /** Key into the sample values (e.g. "resistance"). */
  key: string;
  label: string;
  unit: string;
  /** CSS colour, e.g. "var(--data-r)". Resolved against the plot element. */
  color: string;
  /** Put this trace on the right axis. */
  rightAxis?: boolean;
}

interface Props {
  traces: TraceSpec[];
  /** Seconds of history to show; 0 shows the whole run. */
  windowS?: number;
  /** Compliance markers: shade samples not "OK". */
  showCompliance?: boolean;
  /** Frames per second cap for redraws. */
  fps?: number;
}

export function LivePlot({ traces, windowS = 0, fps = 30 }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const plotRef = useRef<uPlot | null>(null);
  const lastVersion = useRef(-1);

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

    const hasRight = traces.some((t) => t.rightAxis);
    const seriesDefs: Series[] = [
      { label: "t" },
      ...traces.map((t) => ({
        label: t.label,
        stroke: resolve(t.color),
        width: 1.5,
        scale: t.rightAxis ? "y2" : "y",
        spanGaps: false,
        points: { show: false },
        value: (_u: uPlot, v: number | null) => {
          if (v === null || !Number.isFinite(v)) return "—";
          const e = engineering(v, t.unit);
          return `${e.mantissa} ${e.unit}`;
        },
      })),
    ];

    const axisValues = (unit: string) => (_u: uPlot, ticks: number[]) => {
      // Pick one prefix for the whole axis from the largest tick so labels
      // read as a scale, not a jumble of prefixes.
      const largest = ticks.reduce((m, v) => Math.max(m, Math.abs(v)), 0);
      const ref = engineering(largest || 1, unit);
      const factor = largest ? Number(ref.mantissa) / largest : 1;
      return ticks.map((v) => (v * factor).toPrecision(4).replace(/\.?0+$/, "") + (ref.unit ? ` ${ref.unit}` : ""));
    };

    const leftUnit = traces.find((t) => !t.rightAxis)?.unit ?? "";
    const rightUnit = traces.find((t) => t.rightAxis)?.unit ?? "";

    const options: Options = {
      width: host.clientWidth,
      height: host.clientHeight,
      padding: [12, hasRight ? 8 : 16, 0, 8],
      cursor: { drag: { x: true, y: false }, focus: { prox: 16 } },
      // The readout strip above the plot is the live legend; uPlot's own would
      // take height from the canvas for a second copy of the same numbers.
      legend: { show: false },
      scales: { x: { time: false }, y: { auto: true }, ...(hasRight ? { y2: { auto: true } } : {}) },
      axes: [
        {
          stroke: axisText,
          grid: { stroke: gridLine, width: 1 },
          ticks: { stroke: gridLine, width: 1 },
          font: "11px " + computed.getPropertyValue("--font-mono"),
          values: (_u, ticks) => ticks.map((v) => formatAxisTime(v)),
          size: 30,
        },
        {
          scale: "y",
          stroke: axisText,
          grid: { stroke: gridLine, width: 1 },
          ticks: { stroke: gridLine, width: 1 },
          font: "11px " + computed.getPropertyValue("--font-mono"),
          values: axisValues(leftUnit),
          size: 64,
        },
        ...(hasRight
          ? [
              {
                scale: "y2",
                side: 1,
                stroke: axisText,
                grid: { show: false },
                ticks: { stroke: gridLine, width: 1 },
                font: "11px " + computed.getPropertyValue("--font-mono"),
                values: axisValues(rightUnit),
                size: 64,
              } as uPlot.Axis,
            ]
          : []),
      ],
      series: seriesDefs,
    };

    const plot = new uPlot(options, [[]] as unknown as AlignedData, host);
    plotRef.current = plot;
    lastVersion.current = -1;

    const observer = new ResizeObserver(() => {
      plot.setSize({ width: host.clientWidth, height: host.clientHeight });
    });
    observer.observe(host);

    let frame = 0;
    let lastDraw = 0;
    const minInterval = 1000 / fps;
    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      if (now - lastDraw < minInterval) return;
      const version = getSamplesVersion();
      if (version === lastVersion.current) return;
      lastVersion.current = version;
      lastDraw = now;

      const series = getSeries();
      let t = series.t;
      let start = 0;
      if (windowS > 0 && t.length > 0) {
        const cutoff = (t[t.length - 1] ?? 0) - windowS;
        // t is monotonic: binary search for the window start.
        let lo = 0;
        let hi = t.length;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if ((t[mid] ?? 0) < cutoff) lo = mid + 1;
          else hi = mid;
        }
        start = lo;
        t = t.slice(start);
      }
      const columns = traces.map((tr) => {
        const column = series.values[tr.key];
        if (!column) return new Array<number>(t.length).fill(NaN);
        return start ? column.slice(start) : column;
      });
      plot.setData([t, ...columns] as AlignedData);
    };
    frame = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      plot.destroy();
      plotRef.current = null;
    };
    // traces/windowS/fps changes rebuild the plot; that is intended.
  }, [traces, windowS, fps]);

  return <div className={styles.host} ref={hostRef} />;
}

function formatAxisTime(seconds: number): string {
  if (Math.abs(seconds) >= 3600) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}:${String(m).padStart(2, "0")}h`;
  }
  if (Math.abs(seconds) >= 60) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  }
  return `${Number(seconds.toPrecision(3))}s`;
}
