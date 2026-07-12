import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { getService, getServices } from "../src/lib/services";

// getServices reads process.env at call time, so each test sets the env it needs.
const ENV_KEYS = ["PORTAL_SERVICES", "PORTAL_SERVERLESS_API_URL", "PORTAL_STORAGE_API_URL"];

describe("getServices", () => {
  let saved: Record<string, string | undefined>;

  beforeEach(() => {
    saved = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
    for (const k of ENV_KEYS) delete process.env[k];
  });

  afterEach(() => {
    for (const k of ENV_KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  });

  it("disables serverless when no address is configured", async () => {
    expect(getService("serverless")?.enabled).toBe(false);
  });

  it("enables serverless and strips a trailing slash from its address", async () => {
    process.env.PORTAL_SERVERLESS_API_URL = "https://serverless-api.example.com/";

    const svc = getService("serverless");
    expect(svc?.enabled).toBe(true);
    expect(svc?.apiBaseUrl).toBe("https://serverless-api.example.com");
  });

  it("lets PORTAL_SERVICES add a brand-new offering via env only", async () => {
    process.env.PORTAL_SERVICES = JSON.stringify([
      {
        id: "queue",
        name: "Queues",
        apiBaseUrl: "https://queue.example.com",
        category: "Messaging",
      },
    ]);

    const svc = getService("queue");
    expect(svc?.name).toBe("Queues");
    expect(svc?.enabled).toBe(true);
    expect(svc?.category).toBe("Messaging");
  });

  it("ignores malformed PORTAL_SERVICES and keeps the default catalog", async () => {
    process.env.PORTAL_SERVICES = "not-json";

    expect(getServices().some((s) => s.id === "serverless")).toBe(true);
  });
});
