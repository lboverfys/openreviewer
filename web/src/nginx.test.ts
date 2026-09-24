import { readFileSync } from "node:fs";
import ts from "typescript";
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
    "/api/v1/auth/sessions/revoke-all",
    "/api/v1/platform/usage",
    "/api/v1/platform/usage/month-1/requests",
    "/api/v1/platform/usage/month-1/breakdown",
    "/api/v1/platform/work-items",
    "/api/v1/platform/work-items/work-1",
    "/api/v1/platform/work-items/work-1/knowledge",
    "/api/v1/platform/approvals",
    "/api/v1/platform/profiles",
    "/api/v1/platform/profiles/profile-1/activate",
    "/api/v1/platform/diagnostics",
    "/api/v1/platform/audits",
    "/api/v1/evaluations/sources",
    "/api/v1/evaluations/datasets",
    "/api/v1/evaluations/datasets/dataset-1",
    "/api/v1/evaluations/datasets/dataset-1/observations",
    "/api/v1/evaluations/datasets/dataset-1/report",
    "/api/v1/evaluations/datasets/dataset-1/overview",
    "/api/v1/evaluations/datasets/dataset-1/cases",
    "/api/v1/evaluations/datasets/dataset-1/audits",
    "/api/v1/evaluations/datasets/dataset-1/archive",
    "/api/v1/evaluations/cases/case-1",
    "/api/v1/evaluations/cases/case-1/reference",
    "/api/v1/evaluations/cases/case-1/reference/reviews",
    "/api/v1/evaluations/cases/case-1/observations/baseline",
    "/api/v1/evaluations/cases/case-1/observations/candidate/findings",
    "/api/v1/evaluations/cases/case-1/observations/candidate/findings/finding-1/review",
    "/api/v1/evaluations/cases/case-1/observations/candidate/submit",
    "/api/v1/evaluations/cases/case-1/observations/candidate/changes",
    "/api/v1/evaluations/cases/case-1/observations/candidate/source",
    "/api/v1/team/members",
    "/api/v1/team/members/reviewer",
    "/api/v1/team/members/reviewer@example.com",
    "/api/v1/team/repositories",
    "/api/v1/team/repositories/repo-1",
    "/api/v1/team/repositories/repo-1/check",
    "/api/v1/team/github/installations",
    "/api/v1/team/github/installations/123/repositories",
    "/api/v1/team/audits",
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
    "/api/v1/knowledge/documents/789cd0af-e771-4fbe-a544-c36ce9592e64/versions",
    "/api/v1/knowledge/documents/789cd0af-e771-4fbe-a544-c36ce9592e64/versions/2/restore",
  ])("proxies the supported knowledge route %s", (path) => {
    expect(isProxied(path)).toBe(true);
  });

  it.each([
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/actions",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/change-token",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/identity/sync",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/findings/finding-1",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/findings",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/events",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/batches",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/excluded-files",
  ])("proxies the supported review detail route %s", (path) => {
    expect(isProxied(path)).toBe(true);
  });

  it("代理所有已公开的审查详情接口，避免后端新增接口在线上返回 404", () => {
    const openapi = ts.createSourceFile(
      "openapi.ts",
      readFileSync(new URL("./generated/openapi.ts", import.meta.url), "utf8"),
      ts.ScriptTarget.Latest,
    );
    const pathDeclaration = openapi.statements
      .filter(ts.isInterfaceDeclaration)
      .find(declaration => declaration.name.text === "paths");
    expect(pathDeclaration).toBeDefined();
    const paths = pathDeclaration!.members
      .filter(ts.isPropertySignature)
      .map(member => member.name)
      .filter(ts.isStringLiteral)
      .map(name => name.text)
      .filter(path => path.startsWith("/api/v1/reviews/{review_run_id}"));
    expect(paths.length).toBeGreaterThan(0);
    for (const path of paths) {
      expect(isProxied(path.replace(/\{[^}]+\}/g, "789cd0af-e771-4fbe-a544-c36ce9592e64")), path).toBe(true);
    }
  });

  it.each([
    "/api/v1/retrieval/settings",
    "/api/v1/retrieval/targets",
    "/api/v1/retrieval/operations",
    "/api/v1/retrieval/indexes/index-1/enrich",
    "/api/v1/retrieval/settings/test",
    "/api/v1/retrieval/indexes",
    "/api/v1/retrieval/indexes/index-1",
    "/api/v1/retrieval/indexes/index-1/retry",
    "/api/v1/retrieval/indexes/index-1/search",
    "/api/v1/retrieval/indexes/index-1/history",
    "/api/v1/retrieval/indexes/index-1/compare",
    "/api/v1/retrieval/history/search-1",
    "/api/v1/retrieval/evaluations",
    "/api/v1/reviews/review-1/retrieval",
  ])("proxies the supported retrieval route %s", (path) => {
    expect(isProxied(path)).toBe(true);
  });

  it.each([
    "/api/v1/retrieval",
    "/api/v1/platform/internal",
    "/api/v1/platform/profiles/profile-1/delete",
    "/api/v1/platform/usage/month-1/raw-secret",
    "/api/v1/retrieval/settings/delete",
    "/api/v1/retrieval/indexes/index-1/delete",
    "/api/v1/settings",
    "/api/v1/settings/ai/providers/custom",
    "/api/v1/settings/ai/providers/openai/delete",
    "/api/v1/settings/ai/agents/custom",
    "/api/v1/settings/ai/agents/security/delete",
    "/api/v1/settings/audits/export",
    "/api/v1/knowledge",
    "/api/v1/knowledge/project-pack",
    "/api/v1/team/github/installations/0/repositories",
    "/api/v1/team/github/installations/123/delete",
    "/api/v1/team/repositories/repo-1/delete",
    "/api/v1/retrieval/history/search-1/delete",
    "/api/v1/knowledge/documents/all/delete",
    "/api/v1/knowledge/documents/document-1/versions/0/restore",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/unknown",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/batches/delete",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/events/delete",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/excluded-files/delete",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/identity/delete",
    "/api/v1/reviews/789cd0af-e771-4fbe-a544-c36ce9592e64/findings/finding-1/delete",
  ])("does not proxy the unsupported settings route %s", (path) => {
    expect(isProxied(path)).toBe(false);
  });

  it("keeps the catch-all API denial", () => {
    expect(nginxConfig).toMatch(/location \/api\/\s*\{\s*return 404;/);
  });

  it("overwrites the forwarded authority for the API origin guard", () => {
    const proxyBlocks = Array.from(
      nginxConfig.matchAll(/location[^\{]*\{([^}]*)\}/g),
    )
      .map((match) => match[1])
      .filter((block) => block.includes("proxy_pass http://openreviewer_api;"));
    expect(proxyBlocks.length).toBeGreaterThan(0);
    expect(proxyBlocks.every((block) =>
      block.includes("proxy_set_header X-Forwarded-Proto $scheme;") &&
      block.includes("proxy_set_header X-Forwarded-Host $http_host;")
    )).toBe(true);
  });
});
