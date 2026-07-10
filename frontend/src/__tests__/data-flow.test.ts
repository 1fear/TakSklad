import { afterEach, describe, expect, it, vi } from "vitest";

import {
  RequestCoordinator,
  SEARCH_DEBOUNCE_MS,
  TtlCache,
  createDebouncedTask,
} from "../data-flow";

afterEach(() => {
  vi.useRealTimers();
});

describe("request generation ownership", () => {
  it("prevents a delayed old response from overwriting the latest response", () => {
    const coordinator = new RequestCoordinator();
    const committed: string[] = [];
    const oldRequest = coordinator.begin("orders");
    const latestRequest = coordinator.begin("orders");

    expect(oldRequest.signal.aborted).toBe(true);
    expect(latestRequest.commit("latest", (value) => committed.push(value))).toBe(true);
    expect(oldRequest.commit("old", (value) => committed.push(value))).toBe(false);
    expect(committed).toEqual(["latest"]);
  });

  it("allows at most one active request per resource and aborts superseded work", () => {
    const coordinator = new RequestCoordinator();
    const orders = coordinator.begin("orders");
    const incidents = coordinator.begin("incidents");
    expect(coordinator.activeCount).toBe(2);

    const replacement = coordinator.begin("orders");
    expect(orders.signal.aborted).toBe(true);
    expect(replacement.signal.aborted).toBe(false);
    expect(incidents.signal.aborted).toBe(false);
    expect(coordinator.activeCount).toBe(2);

    replacement.finish();
    expect(coordinator.isActive("orders")).toBe(false);
    expect(coordinator.activeCount).toBe(1);
  });

  it("aborts all protected requests on logout and permanently revokes their commits", () => {
    const coordinator = new RequestCoordinator();
    const orders = coordinator.begin("orders");
    const incidents = coordinator.begin("incidents");

    coordinator.clear();

    expect(orders.signal.aborted).toBe(true);
    expect(incidents.signal.aborted).toBe(true);
    expect(coordinator.activeCount).toBe(0);
    expect(orders.canCommit()).toBe(false);
    expect(incidents.canCommit()).toBe(false);
  });

  it("lets an owning lease abort itself without cancelling a newer generation", () => {
    const coordinator = new RequestCoordinator();
    const oldRequest = coordinator.begin("orders");
    const latestRequest = coordinator.begin("orders");

    oldRequest.abort();

    expect(oldRequest.signal.aborted).toBe(true);
    expect(latestRequest.signal.aborted).toBe(false);
    expect(coordinator.isActive("orders")).toBe(true);
  });
});

describe("search debounce", () => {
  it("uses 275 ms and executes only the latest scheduled search", () => {
    vi.useFakeTimers();
    const search = vi.fn();
    const debounced = createDebouncedTask(search);

    debounced.schedule("a");
    vi.advanceTimersByTime(200);
    debounced.schedule("latest");
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 1);
    expect(search).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);

    expect(search).toHaveBeenCalledTimes(1);
    expect(search).toHaveBeenCalledWith("latest");
    expect(debounced.pending).toBe(false);
  });

  it("supports explicit cancel and flush while enforcing the 250-300 ms contract", () => {
    vi.useFakeTimers();
    const search = vi.fn();
    const debounced = createDebouncedTask(search, 250);
    debounced.schedule("cancelled");
    debounced.cancel();
    vi.runAllTimers();
    expect(search).not.toHaveBeenCalled();

    debounced.schedule("flushed");
    debounced.flush();
    expect(search).toHaveBeenCalledWith("flushed");
    expect(() => createDebouncedTask(search, 301)).toThrow(RangeError);
  });
});

describe("protected TTL cache", () => {
  it("expires deterministically and supports mutation/logout invalidation", () => {
    let now = 10_000;
    const cache = new TtlCache<string, string>(1_000, () => now);
    cache.set("dashboard", "cached");
    expect(cache.get("dashboard")).toBe("cached");

    now = 11_000;
    expect(cache.get("dashboard")).toBeUndefined();
    expect(cache.size).toBe(0);

    cache.set("orders", "v1");
    expect(cache.invalidate("orders")).toBe(true);
    cache.set("incidents", "private");
    cache.clear();
    expect(cache.size).toBe(0);
  });
});
