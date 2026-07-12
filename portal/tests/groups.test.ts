import { describe, expect, it } from "vitest";

import { normalizeGroup, normalizeGroups } from "../src/lib/groups";

// Mirrors the Serverless API's group normalization semantics
// (api/models/common.py::normalize_group) so identity stays aligned.
describe("normalizeGroup", () => {
  it("strips the Keycloak path prefix", () => {
    expect(normalizeGroup("/platforms")).toBe("platforms");
  });

  it("strips the ggd-<digits>- prefix", () => {
    expect(normalizeGroup("ggd-1234-platforms")).toBe("platforms");
  });

  it("strips both the path and ggd prefixes together", () => {
    expect(normalizeGroup("/ggd-1234-platforms")).toBe("platforms");
  });

  it("only accepts 1-4 digit ggd prefixes", () => {
    expect(normalizeGroup("ggd-12345-team")).toBe("ggd-12345-team");
  });

  it("leaves a bare group unchanged", () => {
    expect(normalizeGroup("platforms")).toBe("platforms");
  });
});

describe("normalizeGroups", () => {
  it("normalizes, de-duplicates, and sorts", () => {
    expect(normalizeGroups(["/ggd-1-team", "team", "/alpha"])).toEqual(["alpha", "team"]);
  });

  it("coerces a single string to a list", () => {
    expect(normalizeGroups("/ggd-99-solo")).toEqual(["solo"]);
  });

  it("returns an empty array for non-list, non-string input", () => {
    expect(normalizeGroups(undefined)).toEqual([]);
    expect(normalizeGroups(null)).toEqual([]);
    expect(normalizeGroups(42)).toEqual([]);
  });

  it("drops empty and non-string entries", () => {
    expect(normalizeGroups(["", "team", 5, null])).toEqual(["team"]);
  });
});
