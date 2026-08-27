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
    "/api/v1/settings/ai/agents",
    "/api/v1/settings/ai/agents/security",
    "/api/v1/settings/ai/agents/convention/test",
    "/api/v1/settings/ai/agents/logic/enabled",
    "/api/v1/settings/ai/agents/summary",
    "/api/v1/settings/audits",
  ])("proxies the supported settings route %s", (path) => {
    expect(isProxied(path)).toBe(true);
  });

  it.each([
    "/api/v1/knowledge/search",
    "/api/v1/knowledge/documents",
    "/api/v1/knowledge/documents/789cd0af-e771-4fbe-a544-c36ce9592e64",
    "/api/v1/knowledge/documents/789cd0af-e771-4fbe-a544-c36ce9592e64/archive",
    "/api/v1/knowledge/documents/789cd0af-e771-4fbe-a544-c36ce9592e64/restore",
    "/api/v1/knowledge/documents/789cd0af-e771-4fbe-a544-c36ce9592e64/versions/2/restore",
  ])("proxies the supported knowledge route %s", (path) => {
    expect(isProxied(path)).toBe(true);
  });

  it.each([
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/actions",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/findings/finding-1",
  ])("proxies the supported review detail route %s", (path) => {
    expect(isProxied(path)).toBe(true);
  });

  it.each([
    "/api/v1/settings",
    "/api/v1/settings/ai/providers/custom",
    "/api/v1/settings/ai/providers/openai/delete",
    "/api/v1/settings/ai/agents/custom",
    "/api/v1/settings/ai/agents/security/delete",
    "/api/v1/settings/audits/export",
    "/api/v1/knowledge",
    "/api/v1/knowledge/documents/all/delete",
    "/api/v1/knowledge/documents/document-1/versions/0/restore",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/unknown",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/findings",
  ])("does not proxy the unsupported settings route %s", (path) => {
    expect(isProxied(path)).toBe(false);
  });

  it("keeps the catch-all API denial", () => {
    expect(nginxConfig).toMatch(/location \/api\/\s*\{\s*return 404;/);
  });
});
