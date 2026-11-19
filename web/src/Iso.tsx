/** Isometric teal bars and the Sentinel mark: drawn here, as SVG, so nothing is borrowed. */

export function IsoBar({ length = 160, size = 34, style }: { length?: number; size?: number; style?: React.CSSProperties }) {
  // A rectangular prism seen from above-left: top face, front face, end face.
  const dx = length * 0.866, dy = length * 0.5, s = size, h = size * 0.9;
  const top = `0,${s * 0.5} ${s * 0.866},0 ${s * 0.866 + dx},${dy} ${dx},${dy + s * 0.5}`;
  const front = `0,${s * 0.5} ${dx},${dy + s * 0.5} ${dx},${dy + s * 0.5 + h} 0,${s * 0.5 + h}`;
  const end = `${dx},${dy + s * 0.5} ${s * 0.866 + dx},${dy} ${s * 0.866 + dx},${dy + h} ${dx},${dy + s * 0.5 + h}`;
  const w = s * 0.866 + dx + 2, H = dy + s * 0.5 + h + 2;
  return (
    <svg className="iso" style={style} width={w} height={H} viewBox={`0 0 ${w} ${H}`} aria-hidden="true">
      <polygon points={top} fill="#5ee8d6" />
      <polygon points={front} fill="#16b9a5" />
      <polygon points={end} fill="#0d8e7f" />
    </svg>
  );
}

export function Mark({ size = 30 }: { size?: number }) {
  // Three linked blocks: a tiny chain.
  const cube = (x: number, y: number, c: string) => (
    <g transform={`translate(${x} ${y})`}>
      <polygon points="0,4 7,0 14,4 7,8" fill="#5ee8d6" />
      <polygon points="0,4 7,8 7,16 0,12" fill={c} />
      <polygon points="7,8 14,4 14,12 7,16" fill="#0d8e7f" />
    </g>
  );
  return (
    <svg width={size} height={size} viewBox="0 0 30 30" aria-hidden="true">
      {cube(1, 10, "#16b9a5")}
      {cube(8, 6, "#16b9a5")}
      {cube(15, 2, "#ffffff")}
    </svg>
  );
}
