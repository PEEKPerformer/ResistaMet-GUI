// Inline icon set. Sixteen-unit grid, stroke-based, currentColor — so they
// take the text colour of wherever they sit and need no asset pipeline.

import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 16, children, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const Icons = {
  resistance: (p: IconProps) => (
    <Svg {...p}>
      <path d="M3.5 12.5h2.2l1.1-2.6a3.2 3.2 0 1 1 2.4 0l1.1 2.6h2.2" />
    </Svg>
  ),
  voltage: (p: IconProps) => (
    <Svg {...p}>
      <path d="M8.8 1.8 4.5 9h3.2l-.7 5.2 4.5-7.2H8.3z" />
    </Svg>
  ),
  current: (p: IconProps) => (
    <Svg {...p}>
      <path d="M2.5 8h9.5M9 4.5 12.5 8 9 11.5" />
      <path d="M2.5 4.5v7" />
    </Svg>
  ),
  fourPoint: (p: IconProps) => (
    <Svg {...p}>
      <path d="M2.5 11.5h11" />
      <path d="M4 11.5V7M6.7 11.5V7M9.3 11.5V7M12 11.5V7" />
      <circle cx="4" cy="5.2" r="1" fill="currentColor" stroke="none" />
      <circle cx="6.7" cy="5.2" r="1" fill="currentColor" stroke="none" />
      <circle cx="9.3" cy="5.2" r="1" fill="currentColor" stroke="none" />
      <circle cx="12" cy="5.2" r="1" fill="currentColor" stroke="none" />
    </Svg>
  ),
  sweep: (p: IconProps) => (
    <Svg {...p}>
      <path d="M2.5 13.5v-11M2.5 13.5h11" />
      <path d="M4 11.5c3-0.5 5.5-3 7.5-7.5" />
    </Svg>
  ),
  vdp: (p: IconProps) => (
    <Svg {...p}>
      <path d="M4 4h8v8H4z" />
      <circle cx="4" cy="4" r="1.3" fill="currentColor" stroke="none" />
      <circle cx="12" cy="4" r="1.3" fill="currentColor" stroke="none" />
      <circle cx="4" cy="12" r="1.3" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none" />
    </Svg>
  ),
  results: (p: IconProps) => (
    <Svg {...p}>
      <path d="M2.5 3.5h4l1.5 1.5h5.5v7.5h-11z" />
      <path d="M5.5 10.5l1.8-2 1.6 1.2 2.2-2.7" />
    </Svg>
  ),
  play: (p: IconProps) => (
    <Svg {...p}>
      <path d="M4.5 3.2v9.6L12.5 8z" fill="currentColor" stroke="none" />
    </Svg>
  ),
  pause: (p: IconProps) => (
    <Svg {...p}>
      <path d="M4.5 3.5h2.5v9H4.5zM9 3.5h2.5v9H9z" fill="currentColor" stroke="none" />
    </Svg>
  ),
  stop: (p: IconProps) => (
    <Svg {...p}>
      <rect x="3.5" y="3.5" width="9" height="9" rx="1.2" fill="currentColor" stroke="none" />
    </Svg>
  ),
  flag: (p: IconProps) => (
    <Svg {...p}>
      <path d="M4 14V2.5M4 3h7.5l-1.5 2.5 1.5 2.5H4" />
    </Svg>
  ),
  settings: (p: IconProps) => (
    <Svg {...p}>
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 1.8v1.7M8 12.5v1.7M1.8 8h1.7M12.5 8h1.7M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2M3.6 12.4l1.2-1.2M11.2 4.8l1.2-1.2" />
    </Svg>
  ),
  user: (p: IconProps) => (
    <Svg {...p}>
      <circle cx="8" cy="5.5" r="2.7" />
      <path d="M2.8 14c.6-3 2.7-4.3 5.2-4.3s4.6 1.3 5.2 4.3" />
    </Svg>
  ),
  plug: (p: IconProps) => (
    <Svg {...p}>
      <path d="M5.5 2v3M10.5 2v3M4 5h8v2.5a4 4 0 0 1-8 0zM8 11.5V14" />
    </Svg>
  ),
  chevronDown: (p: IconProps) => (
    <Svg {...p}>
      <path d="M4 6.5 8 10.5l4-4" />
    </Svg>
  ),
  chevronUp: (p: IconProps) => (
    <Svg {...p}>
      <path d="M4 10 8 6l4 4" />
    </Svg>
  ),
  close: (p: IconProps) => (
    <Svg {...p}>
      <path d="M4 4l8 8M12 4l-8 8" />
    </Svg>
  ),
  warning: (p: IconProps) => (
    <Svg {...p}>
      <path d="M8 2.5 14 13H2z" />
      <path d="M8 6.5v3M8 11.6v.1" />
    </Svg>
  ),
  check: (p: IconProps) => (
    <Svg {...p}>
      <path d="M3.5 8.5 6.5 11.5 12.5 5" />
    </Svg>
  ),
  trash: (p: IconProps) => (
    <Svg {...p}>
      <path d="M3 4.5h10M6 4.5V3h4v1.5M4.5 4.5l.7 8.5h5.6l.7-8.5" />
    </Svg>
  ),
  folder: (p: IconProps) => (
    <Svg {...p}>
      <path d="M2.5 3.5h4l1.5 1.5h5.5v7.5h-11z" />
    </Svg>
  ),
};

export type IconName = keyof typeof Icons;
