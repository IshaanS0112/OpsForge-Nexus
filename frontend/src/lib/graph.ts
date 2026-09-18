// Layout and reasoning for the dependency graph. Edges are
// {service, depends_on}: ``service`` calls ``depends_on``.

export interface Edge {
  service: string;
  depends_on: string;
}

export interface PlacedNode {
  name: string;
  col: number;
  row: number;
  x: number;
  y: number;
}

export interface Layout {
  nodes: Map<string, PlacedNode>;
  width: number;
  height: number;
}

export interface LayoutOptions {
  nodeW: number;
  nodeH: number;
  colGap: number;
  rowGap: number;
  pad: number;
}

/**
 * Tiered layout: callers on the left, what they depend on to the right (the
 * data tier ends up rightmost). Rows within a column are ordered by the
 * barycentre of their neighbours in the adjacent column, sweeping both ways,
 * which removes the edge crossings a name sort would leave.
 */
export function layoutGraph(services: { name: string; layer: number }[], edges: Edge[], o: LayoutOptions): Layout {
  const maxLayer = Math.max(0, ...services.map((s) => s.layer));
  const cols: string[][] = Array.from({ length: maxLayer + 1 }, () => []);
  for (const s of [...services].sort((a, b) => a.name.localeCompare(b.name))) cols[maxLayer - s.layer].push(s.name);

  const callers = new Map<string, string[]>();
  const callees = new Map<string, string[]>();
  for (const e of edges) {
    callees.set(e.service, [...(callees.get(e.service) ?? []), e.depends_on]);
    callers.set(e.depends_on, [...(callers.get(e.depends_on) ?? []), e.service]);
  }
  const rowOf = () => new Map(cols.flatMap((c) => c.map((n, i) => [n, i] as const)));
  const bary = (neighbours: string[] | undefined, rows: Map<string, number>, fallback: number) => {
    const rs = (neighbours ?? []).map((m) => rows.get(m)).filter((r): r is number => r !== undefined);
    return rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : fallback;
  };
  for (let sweep = 0; sweep < 4; sweep++) {
    const forward = sweep % 2 === 0;
    const order = forward ? cols.map((_, i) => i).slice(1) : cols.map((_, i) => i).slice(0, -1).reverse();
    for (const c of order) {
      const rows = rowOf();
      const key = new Map(cols[c].map((n, i) => [n, bary(forward ? callers.get(n) : callees.get(n), rows, i)]));
      cols[c].sort((a, b) => (key.get(a)! - key.get(b)!) || a.localeCompare(b));
    }
  }

  const tallest = Math.max(1, ...cols.map((c) => c.length));
  const height = o.pad * 2 + tallest * o.nodeH + (tallest - 1) * o.rowGap;
  const width = o.pad * 2 + cols.length * o.nodeW + (cols.length - 1) * o.colGap;
  const nodes = new Map<string, PlacedNode>();
  cols.forEach((col, c) => {
    const colHeight = col.length * o.nodeH + (col.length - 1) * o.rowGap;
    const top = o.pad + (height - 2 * o.pad - colHeight) / 2;
    col.forEach((name, r) => {
      nodes.set(name, { name, col: c, row: r, x: o.pad + c * (o.nodeW + o.colGap), y: top + r * (o.nodeH + o.rowGap) });
    });
  });
  return { nodes, width, height };
}

/** Everything that can be hurt when ``service`` fails: BFS over dependents. */
export function blastRadius(service: string, edges: Edge[]): string[] {
  const dependents = new Map<string, string[]>();
  for (const e of edges) dependents.set(e.depends_on, [...(dependents.get(e.depends_on) ?? []), e.service]);
  const seen = new Set([service]);
  const order: string[] = [];
  const queue = [...(dependents.get(service) ?? [])].sort();
  while (queue.length) {
    const n = queue.shift()!;
    if (seen.has(n)) continue;
    seen.add(n);
    order.push(n);
    queue.push(...[...(dependents.get(n) ?? [])].sort());
  }
  return order;
}

/**
 * Edges a live cascade is travelling along: the caller's incident cascades
 * from elsewhere and the service it calls is itself in trouble.
 */
export function cascadeEdges(
  services: { name: string; incident: { is_cascading: boolean } | null }[],
  edges: Edge[],
): Set<string> {
  const inc = new Map(services.map((s) => [s.name, s.incident]));
  const out = new Set<string>();
  for (const e of edges) {
    const caller = inc.get(e.service);
    const callee = inc.get(e.depends_on);
    if (caller?.is_cascading && callee) out.add(edgeKey(e));
  }
  return out;
}

export const edgeKey = (e: Edge) => `${e.service}->${e.depends_on}`;

/** Smooth S-curve from the caller's right edge to the callee's left edge. */
export function edgePath(from: PlacedNode, to: PlacedNode, nodeW: number, nodeH: number): string {
  const x1 = from.x + nodeW;
  const y1 = from.y + nodeH / 2;
  const x2 = to.x;
  const y2 = to.y + nodeH / 2;
  const mid = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2 - 6} ${y2}`;
}
