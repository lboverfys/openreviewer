import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const nginxConfig = readFileSync(new URL("../nginx.conf", import.meta.url), "utf8");

function regexProxyLocations(config: string): RegExp[] {
  return Array.from(
    config.matchAll(/location\s+~\s+([^\s{]+)\s*\{([^}]*)\}/g),
  )
    .filter((match) => match[2].includes("proxy_pass http://openreviewer_api;"))
    .map((match) => new RegExp(match[1]));
}

describe("Nginx API allowlist", () => {
  const proxyLocations = regexProxyLocations(nginxConfig);
  const isProxied = (path: string) =>
    proxyLocations.some((location) => location.test(path));

  it.each([
    "/api/v1/settings/ai",
    "/api/v1/settings/ai/providers/openai",
    "/api/v1/settings/ai/providers/openai/test",
    "/api/v1/settings/ai/providers/openai/activate",
    "/api/v1/settings/ai/providers/anthropic",
    "/api/v1/settings/ai/providers/anthropic/test",
    "/api/v1/settings/ai/providers/anthropic/activate",
    "/api/v1/settings/ai/review-policy",
    "/api/v1/settings/audits",
  ])("proxies the supported settings route %s", (path) => {
    expect(isProxied(path)).toBe(true);
  });

  it.each([
    "/api/v1/settings",
    "/api/v1/settings/ai/providers/custom",
    "/api/v1/settings/ai/providers/openai/delete",
    "/api/v1/settings/audits/export",
  ])("does not proxy the unsupported settings route %s", (path) => {
    expect(isProxied(path)).toBe(false);
  });

  it("keeps the catch-all API denial", () => {
    expect(nginxConfig).toMatch(/location \/api\/\s*\{\s*return 404;/);
  });
});
