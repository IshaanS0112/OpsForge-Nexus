import { describe, expect, it } from "vitest";
import { blastRadius, cascadeEdges, layoutGraph, type Edge } from "./graph";

const EDGES: Edge[] = [
  { service: "notification-svc", depends_on: "checkout-api" },
  { service: "checkout-api", depends_on: "payments-svc" },
  { service: "checkout-api", depends_on: "inventory-svc" },
  { service: "payments-svc", depends_on: "ledger-svc" },
  { service: "inventory-svc", depends_on: "catalog-svc" },
];
const SERVICES = [
  { name: "notification-svc", layer: 3 },
  { name: "checkout-api", layer: 2 },
  { name: "payments-svc", layer: 1 },
  { name: "inventory-svc", layer: 1 },
  { name: "ledger-svc", layer: 0 },
  { name: "catalog-svc", layer: 0 },
];
const OPTS = { nodeW: 100, nodeH: 40, colGap: 20, rowGap: 10, pad: 5 };

describe("layoutGraph", () => {
  it("puts callers left of what they call", () => {
    const { nodes } = layoutGraph(SERVICES, EDGES, OPTS);
    for (const e of EDGES) expect(nodes.get(e.service)!.x).toBeLessThan(nodes.get(e.depends_on)!.x);
  });

  it("orders rows so dependency chains do not cross", () => {
    const { nodes } = layoutGraph(SERVICES, EDGES, OPTS);
    const payments = nodes.get("payments-svc")!.row < nodes.get("inventory-svc")!.row;
    const ledger = nodes.get("ledger-svc")!.row < nodes.get("catalog-svc")!.row;
    expect(payments).toBe(ledger);
  });

  it("sizes the canvas to the widest and tallest column", () => {
    const { width, height } = layoutGraph(SERVICES, EDGES, OPTS);
    expect(width).toBe(5 * 2 + 4 * 100 + 3 * 20);
    expect(height).toBe(5 * 2 + 2 * 40 + 10);
  });
});

describe("blastRadius", () => {
  it("walks dependents breadth-first, nearest first", () => {
    expect(blastRadius("ledger-svc", EDGES)).toEqual(["payments-svc", "checkout-api", "notification-svc"]);
    expect(blastRadius("notification-svc", EDGES)).toEqual([]);
  });
});

describe("cascadeEdges", () => {
  it("lights edges from a cascading caller into a service that is itself in trouble", () => {
    const services = [
      { name: "ledger-svc", incident: { is_cascading: false } },
      { name: "payments-svc", incident: { is_cascading: true } },
      { name: "checkout-api", incident: { is_cascading: true } },
      { name: "inventory-svc", incident: null },
      { name: "notification-svc", incident: null },
      { name: "catalog-svc", incident: null },
    ];
    const hot = cascadeEdges(services, EDGES);
    expect([...hot].sort()).toEqual(["checkout-api->payments-svc", "payments-svc->ledger-svc"]);
  });
});
